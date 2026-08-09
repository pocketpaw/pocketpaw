// entry.js — the SSR render surface of the once-built site renderer.
// Created for SG-1 (sites proving harness).
//
// WHAT: exports `renderPage(spec, props)`, which runs `svelte/server`'s
// `render()` over the compiled Page component and returns `{head, body}`.
// This module is the Vite SSR build's entry, so the built artifact carries the
// Ripple widget registry and every widget component compiled for the server.
//
// WHY the HTML shell is NOT assembled here: the per-site tokens (site title,
// primary colour, capture base, signed key, D1 id) are the caller's knowledge,
// and keeping them out of the JS keeps this artifact SPEC-ONLY — one bundle, no
// per-site variation. The Python side substitutes tokens into the app.html
// shell, mirroring what the generator's token pass did.
//
// `rendererMeta.ripple` is filled by the build (see build.mjs), which writes
// ripple-version.js from the version actually installed — no guessing.
import { render } from 'svelte/server';

import Page from './Page.svelte';
import { RIPPLE_VERSION, RIPPLE_SOURCE } from './ripple-version.js';

export const rendererMeta = {
  ripple_version: RIPPLE_VERSION,
  ripple_source: RIPPLE_SOURCE,
};

/**
 * Render one spec through the prebuilt component.
 *
 * @param {unknown} spec        the ripple spec (any shape; Ripple normalizes)
 * @param {{formAction?: string}} [props]
 * @returns {{head: string, body: string}}
 */
export function renderPage(spec, props = {}) {
  const { head, body } = render(Page, {
    props: { spec, formAction: props.formAction ?? '/api/submit' },
  });
  return { head, body };
}

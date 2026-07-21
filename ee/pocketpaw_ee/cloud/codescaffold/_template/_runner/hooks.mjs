// hooks.mjs — resolve TypeScript's `./x.js` convention to the `./x.ts` on disk.
//
// Created 2026-07-21 (CS-1). Fifteen lines that remove the entire Node
// dependency tree from this vendored engine.
//
// The recipe engine is TypeScript and follows the TS convention of importing
// `./engine.js` from `engine.ts` — the specifier names the file the compiler
// WOULD emit. Node's `--experimental-strip-types` runs the `.ts` files happily
// but does not do that remap, so the first relative import fails with
// ERR_MODULE_NOT_FOUND. `tsx` exists largely to paper over this.
//
// Rather than vendor `tsx` (which bundles a per-platform esbuild binary — a
// native artifact in a Python wheel, chosen on the wrong OS half the time), this
// hook does the one thing that was missing. The composed output is byte-identical
// to the tsx-driven run; that was verified before this file was written, not
// assumed.
//
// The remap is deliberately NARROW: relative specifiers only, `.js` only, and
// only when the sibling `.ts` actually exists. A real `.js` file next door still
// wins, because the fallback path is never taken when the `.ts` is absent.

import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

export async function resolve(specifier, context, next) {
  if (specifier.startsWith('.') && specifier.endsWith('.js') && context.parentURL) {
    try {
      const url = new URL(specifier.slice(0, -3) + '.ts', context.parentURL);
      if (existsSync(fileURLToPath(url))) return { url: url.href, shortCircuit: true };
    } catch {
      // Fall through to Node's resolver — a specifier we cannot parse is not ours.
    }
  }
  return next(specifier, context);
}

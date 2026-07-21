// compose.mjs — the ONE entry point the Python backend invokes.
//
// Created 2026-07-21 (CS-1). Reads the base template, applies the requested
// recipes, and prints the composed project to stdout as JSON. Writes nothing to
// disk, takes no output directory, and exits non-zero with a JSON error on
// failure.
//
//   node --experimental-strip-types --import ./_runner/register.mjs \
//        ./_runner/compose.mjs auth stripe
//
//   -> {"ok":true,"order":[...],"secrets":[...],"files":{"<path>":"<contents>"},
//       "plan":[...]}
//
// WHY NOT THE BUNDLED CLI. The template ships `recipe-engine/src/cli.ts`, which
// writes a directory and prints a human-readable plan. Both are wrong for this
// caller: the backend wants a source map it can hand to a runtime (tar it for
// Daytona, `fs.mount` it for a WebContainer), and parsing a table meant for a
// terminal is how you get a scaffolder that breaks on a copy edit. `SourceMap`
// is already `Record<string, string>`, so JSON is not a translation — it is the
// engine's own type, printed.
//
// WHY STATIC IMPORTS OF THE MANIFESTS. The engine's `loadRecipeClosure` finds
// recipes by dynamic `import()` of a computed path. That works, but it means the
// set of recipes is whatever happens to be on disk — so a stray directory
// becomes a callable recipe. Importing the three manifests by name makes the
// catalog a declaration in source, checkable by eye and by the Python side's own
// list. Adding a recipe is then a deliberate two-line edit here, which is the
// correct amount of friction for something that writes code into a user's
// project. The engine anticipated this: `resolveRecipes` exists for exactly the
// case where manifests are imported statically rather than discovered.
//
// The payload files (`recipes/<id>/files/**`) are still read from disk by
// `resolveRecipes` — only the manifests are static.

import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { applyRecipes, resolveOrder } from '../recipe-engine/src/engine.ts';
import { resolveRecipes } from '../recipe-engine/src/registry.ts';
import { readSourceMap } from '../recipe-engine/src/sourcemap.ts';
import { RecipeError } from '../recipe-engine/src/types.ts';

import { recipe as db } from '../recipes/db/recipe.ts';
import { recipe as auth } from '../recipes/auth/recipe.ts';
import { recipe as stripe } from '../recipes/stripe/recipe.ts';

/** The catalog. Adding a recipe means adding it here AND to the Python
 *  domain's catalog — two edits, on purpose: one decides what the engine can
 *  apply, the other decides what a prompt is allowed to ask for. */
const MANIFESTS = [db, auth, stripe];

const TEMPLATE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const BASE_DIR = path.join(TEMPLATE_ROOT, 'base');
const RECIPES_DIR = path.join(TEMPLATE_ROOT, 'recipes');

/** Print a JSON envelope and exit. One shape for success and failure, so the
 *  Python side never has to distinguish "crashed" from "refused". */
function emit(payload, code) {
  process.stdout.write(JSON.stringify(payload));
  process.exit(code);
}

async function main() {
  const requested = process.argv.slice(2).filter((a) => !a.startsWith('-'));
  if (requested.length === 0) {
    // Not an error: the base template on its own is a valid project, and asking
    // for "no features" should compose rather than fail.
    const files = await readSourceMap(BASE_DIR);
    emit({ ok: true, order: [], secrets: [], files, plan: [] }, 0);
  }

  const unknown = requested.filter((id) => !MANIFESTS.some((m) => m.id === id));
  if (unknown.length) {
    emit({ ok: false, error: `unknown recipe(s): ${unknown.join(', ')}` }, 2);
  }

  const source = await readSourceMap(BASE_DIR);
  const byId = await resolveRecipes(RECIPES_DIR, MANIFESTS);
  // `resolveOrder` walks `requires` transitively and returns ONLY what is
  // reachable from `requested` — so passing the whole catalog does not compose
  // the whole catalog. Asking for `auth` yields `db -> auth`.
  const order = resolveOrder(byId, requested);
  const result = applyRecipes(
    source,
    order.map((id) => byId.get(id)),
  );

  emit(
    {
      ok: true,
      order: result.order,
      // Names only. The template's own contract, and the reason a composed
      // project can be handed around without carrying anything sensitive.
      secrets: result.secrets,
      files: result.sourceMap,
      plan: result.plan,
    },
    0,
  );
}

main().catch((err) => {
  // A RecipeError is the engine refusing cleanly (missing anchor, collision,
  // unknown dependency) and is worth reporting verbatim — it names the file and
  // the anchor. Anything else is a bug here, so it carries its stack.
  const isRecipe = err instanceof RecipeError;
  emit(
    {
      ok: false,
      error: isRecipe ? err.message : String(err?.stack || err),
      kind: isRecipe ? 'recipe' : 'internal',
    },
    isRecipe ? 2 : 1,
  );
});

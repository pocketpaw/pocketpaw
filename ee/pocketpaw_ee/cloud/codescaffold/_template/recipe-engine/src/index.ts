import { applyRecipes, resolveOrder } from './engine.js';
import { loadRecipeClosure } from './registry.js';
import type { ApplyResult, SourceMap } from './types.js';

export * from './types.js';
export { applyRecipes, resolveOrder } from './engine.js';
export { loadRecipeClosure, resolveRecipe, resolveRecipes } from './registry.js';
export { readSourceMap, writeSourceMap, digest } from './sourcemap.js';
export { applySplice, sentinelFor, isSpliceApplied } from './splice.js';
export { mergeDependencies } from './deps.js';
export { listMigrations, nextMigrationNumber, migrationPath } from './migrations.js';

/**
 * Load the requested recipes (+ their `requires`) from `recipesDir` and apply
 * them to `source` in deterministic dependency order. This is the one-call
 * entry point used by the CLI and the tests.
 */
export async function applyToSourceMap(
	source: SourceMap,
	recipesDir: string,
	requested: string[]
): Promise<ApplyResult> {
	const byId = await loadRecipeClosure(recipesDir, requested);
	const order = resolveOrder(byId, requested);
	const recipes = order.map((id) => byId.get(id)!);
	return applyRecipes(source, recipes);
}

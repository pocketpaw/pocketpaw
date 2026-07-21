import {
	RecipeError,
	type ApplyResult,
	type PlanChange,
	type ResolvedRecipe,
	type SourceMap,
	type Splice
} from './types.js';
import { applySplice, isSpliceApplied } from './splice.js';
import { depsAlreadyMerged, mergeDependencies } from './deps.js';
import { findMigrationByName, migrationPath, nextMigrationNumber } from './migrations.js';

const APP_D_TS = 'src/app.d.ts';
const PACKAGE_JSON = 'package.json';

/**
 * Resolve `requested` recipe ids into a deterministic apply order: every
 * recipe's `requires` come before it (dependency order), duplicates collapsed,
 * and roots processed in the requested order. Same input list -> same order.
 *
 * Throws on an unknown id or a dependency cycle (fail closed).
 */
export function resolveOrder(
	recipesById: Map<string, ResolvedRecipe>,
	requested: string[]
): string[] {
	const order: string[] = [];
	const done = new Set<string>();
	const onStack = new Set<string>();

	const visit = (id: string, chain: string[]): void => {
		if (done.has(id)) return;
		if (onStack.has(id)) {
			throw new RecipeError(`dependency cycle: ${[...chain, id].join(' -> ')}`);
		}
		const recipe = recipesById.get(id);
		if (!recipe) {
			const from = chain.length ? ` (required by ${chain[chain.length - 1]})` : '';
			throw new RecipeError(`unknown recipe "${id}"${from}`);
		}
		onStack.add(id);
		for (const dep of recipe.requires ?? []) visit(dep, [...chain, id]);
		onStack.delete(id);
		done.add(id);
		order.push(id);
	};

	for (const id of requested) visit(id, []);
	return order;
}

/** Build the app.d.ts splices implied by a recipe's `envTypes` / `locals`. */
function appDtsSplices(recipe: ResolvedRecipe): Splice[] {
	const splices: Splice[] = [];
	if (recipe.envTypes?.length) {
		splices.push({
			file: APP_D_TS,
			anchor: '// @recipe:env',
			key: 'env',
			payload: recipe.envTypes.join('\n')
		});
	}
	if (recipe.locals?.length) {
		splices.push({
			file: APP_D_TS,
			anchor: '// @recipe:locals',
			key: 'locals',
			payload: recipe.locals.join('\n')
		});
	}
	return splices;
}

/**
 * Apply recipes (already in dependency order) to a source map.
 *
 * Returns a NEW source map plus a plan describing every change. Pure: the input
 * map is never mutated. Fail-closed: on the first problem it throws
 * {@link RecipeError} and returns nothing, so a caller that only persists the
 * returned map never writes a partial result to disk.
 */
export function applyRecipes(source: SourceMap, recipes: ResolvedRecipe[]): ApplyResult {
	const map: SourceMap = { ...source };
	const plan: PlanChange[] = [];
	const secrets = new Set<string>();

	for (const recipe of recipes) {
		for (const s of recipe.secrets ?? []) secrets.add(s);

		// 1. Files written verbatim (new files the recipe owns).
		for (const file of recipe.files ?? []) {
			const existing = map[file.path];
			if (existing !== undefined) {
				if (existing === file.contents) {
					plan.push({ recipe: recipe.id, kind: 'file', path: file.path, detail: 'no-op (identical)', noop: true });
					continue;
				}
				throw new RecipeError(
					`recipe "${recipe.id}": file ${file.path} already exists with different content ` +
						`(collision). A recipe may only create files it owns; edit shared files via splices.`
				);
			}
			map[file.path] = file.contents;
			plan.push({ recipe: recipe.id, kind: 'file', path: file.path, detail: 'write', noop: false });
		}

		// 2. Splices into shared files (including env/locals in app.d.ts).
		const splices = [...(recipe.splices ?? []), ...appDtsSplices(recipe)];
		for (const splice of splices) {
			if (map[splice.file] === undefined) {
				throw new RecipeError(
					`recipe "${recipe.id}": splice target ${splice.file} does not exist in the source map.`
				);
			}
			const kind = splice.file === APP_D_TS && (splice.key === 'env' || splice.key === 'locals')
				? (splice.key as 'env' | 'locals')
				: 'splice';
			if (isSpliceApplied(map[splice.file], recipe.id, splice)) {
				plan.push({ recipe: recipe.id, kind, path: splice.file, detail: `no-op (${splice.key} already applied)`, noop: true });
				continue;
			}
			map[splice.file] = applySplice(map[splice.file], recipe.id, splice);
			plan.push({ recipe: recipe.id, kind, path: splice.file, detail: `insert @ ${splice.anchor}`, noop: false });
		}

		// 3. Dependencies merged into package.json.
		if (map[PACKAGE_JSON] === undefined && (recipe.deps || recipe.devDeps)) {
			throw new RecipeError(`recipe "${recipe.id}": package.json missing from source map.`);
		}
		if (recipe.deps && Object.keys(recipe.deps).length) {
			const already = depsAlreadyMerged(map[PACKAGE_JSON], 'dependencies', recipe.deps);
			map[PACKAGE_JSON] = mergeDependencies(map[PACKAGE_JSON], 'dependencies', recipe.deps);
			plan.push({ recipe: recipe.id, kind: 'deps', path: PACKAGE_JSON, detail: already ? 'no-op (deps present)' : `+deps ${Object.keys(recipe.deps).join(', ')}`, noop: already });
		}
		if (recipe.devDeps && Object.keys(recipe.devDeps).length) {
			const already = depsAlreadyMerged(map[PACKAGE_JSON], 'devDependencies', recipe.devDeps);
			map[PACKAGE_JSON] = mergeDependencies(map[PACKAGE_JSON], 'devDependencies', recipe.devDeps);
			plan.push({ recipe: recipe.id, kind: 'deps', path: PACKAGE_JSON, detail: already ? 'no-op (devDeps present)' : `+devDeps ${Object.keys(recipe.devDeps).join(', ')}`, noop: already });
		}

		// 4. Stacked migration (engine assigns the numeric prefix).
		if (recipe.migration) {
			const existing = findMigrationByName(map, recipe.migration.name);
			if (existing) {
				plan.push({ recipe: recipe.id, kind: 'migration', path: existing.path, detail: 'no-op (migration present)', noop: true });
			} else {
				const path = migrationPath(nextMigrationNumber(map), recipe.migration.name);
				map[path] = recipe.migration.contents;
				plan.push({ recipe: recipe.id, kind: 'migration', path, detail: 'write', noop: false });
			}
		}
	}

	return { sourceMap: map, plan, secrets: [...secrets].sort(), order: recipes.map((r) => r.id) };
}

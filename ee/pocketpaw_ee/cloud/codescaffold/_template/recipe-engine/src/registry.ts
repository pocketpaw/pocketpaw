import { promises as fs } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { RecipeError, type Recipe, type ResolvedRecipe } from './types.js';

/** Directory of a single recipe: `<recipesDir>/<id>`. */
function recipeDir(recipesDir: string, id: string): string {
	return path.join(recipesDir, id);
}

/** Import `recipes/<id>/recipe.ts` and return its `recipe` export. */
async function importManifest(recipesDir: string, id: string): Promise<Recipe> {
	const file = path.join(recipeDir(recipesDir, id), 'recipe.ts');
	try {
		await fs.access(file);
	} catch {
		throw new RecipeError(`recipe "${id}" not found (expected ${file})`);
	}
	const mod = await import(pathToFileURL(file).href);
	const recipe: Recipe | undefined = mod.recipe ?? mod.default;
	if (!recipe || recipe.id !== id) {
		throw new RecipeError(`recipe "${id}": ${file} must export \`recipe\` with id "${id}"`);
	}
	return recipe;
}

/** Read all on-disk payloads referenced by a manifest into memory. */
export async function resolveRecipe(recipesDir: string, recipe: Recipe): Promise<ResolvedRecipe> {
	const dir = recipeDir(recipesDir, recipe.id);
	const read = async (rel: string): Promise<string> => {
		const abs = path.join(dir, ...rel.split('/'));
		try {
			return await fs.readFile(abs, 'utf8');
		} catch {
			throw new RecipeError(`recipe "${recipe.id}": payload file not found: ${rel}`);
		}
	};

	const files = await Promise.all(
		(recipe.files ?? []).map(async (f) => ({ path: f.to, contents: await read(f.from) }))
	);
	const migration = recipe.migration
		? { name: recipe.migration.name, contents: await read(recipe.migration.from) }
		: null;

	const { files: _f, migration: _m, ...rest } = recipe;
	return { ...rest, files, migration };
}

/**
 * Load the requested recipes and everything they transitively `require`,
 * returning a map of id -> resolved recipe (payloads read into memory).
 */
export async function loadRecipeClosure(
	recipesDir: string,
	requested: string[]
): Promise<Map<string, ResolvedRecipe>> {
	const byId = new Map<string, ResolvedRecipe>();
	const queue = [...requested];
	while (queue.length) {
		const id = queue.shift()!;
		if (byId.has(id)) continue;
		const manifest = await importManifest(recipesDir, id);
		const resolved = await resolveRecipe(recipesDir, manifest);
		byId.set(id, resolved);
		for (const dep of resolved.requires ?? []) if (!byId.has(dep)) queue.push(dep);
	}
	return byId;
}

/**
 * Resolve a set of already-imported manifests (payloads read into memory),
 * returning id -> resolved recipe. Useful when manifests are imported
 * statically (e.g. in tests) rather than discovered via dynamic import.
 */
export async function resolveRecipes(
	recipesDir: string,
	manifests: Recipe[]
): Promise<Map<string, ResolvedRecipe>> {
	const byId = new Map<string, ResolvedRecipe>();
	for (const m of manifests) byId.set(m.id, await resolveRecipe(recipesDir, m));
	return byId;
}

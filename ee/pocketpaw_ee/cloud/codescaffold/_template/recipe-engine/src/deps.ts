import { RecipeError } from './types.js';

/** Detect indentation (tab vs N spaces) used by a JSON document; default: tab. */
function detectIndent(json: string): string | number {
	const m = json.match(/\n([\t ]+)"/);
	if (!m) return '\t';
	return m[1].includes('\t') ? '\t' : m[1].length;
}

/** Detect whether a JSON document ends with a trailing newline. */
function hasTrailingNewline(json: string): boolean {
	return json.endsWith('\n');
}

/**
 * Merge `additions` into the given dependency block of a package.json string,
 * returning the new package.json text. Keys are sorted; existing versions are
 * overwritten only if different. Pure and idempotent: merging the same additions
 * twice yields byte-identical output.
 */
export function mergeDependencies(
	packageJson: string,
	block: 'dependencies' | 'devDependencies',
	additions: Record<string, string>
): string {
	if (Object.keys(additions).length === 0) return packageJson;

	let pkg: Record<string, unknown>;
	try {
		pkg = JSON.parse(packageJson);
	} catch (e) {
		throw new RecipeError(`could not parse package.json: ${(e as Error).message}`);
	}

	const current = (pkg[block] as Record<string, string> | undefined) ?? {};
	const merged: Record<string, string> = { ...current, ...additions };
	const sorted: Record<string, string> = {};
	for (const key of Object.keys(merged).sort()) sorted[key] = merged[key];
	pkg[block] = sorted;

	const indent = detectIndent(packageJson);
	const out = JSON.stringify(pkg, null, indent);
	return hasTrailingNewline(packageJson) ? out + '\n' : out;
}

/** True if every addition is already present with the same version (idempotent no-op check). */
export function depsAlreadyMerged(
	packageJson: string,
	block: 'dependencies' | 'devDependencies',
	additions: Record<string, string>
): boolean {
	if (Object.keys(additions).length === 0) return true;
	let pkg: Record<string, unknown>;
	try {
		pkg = JSON.parse(packageJson);
	} catch {
		return false;
	}
	const current = (pkg[block] as Record<string, string> | undefined) ?? {};
	return Object.entries(additions).every(([k, v]) => current[k] === v);
}

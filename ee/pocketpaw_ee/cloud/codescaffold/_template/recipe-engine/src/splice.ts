import { RecipeError, type Splice } from './types.js';

/**
 * The idempotency sentinel for a splice, emitted as a `//` comment alongside the
 * inserted payload. Presence of this exact string in the file means the splice
 * has already been applied, so re-applying is a no-op.
 */
export function sentinelFor(recipeId: string, key: string): string {
	return `// @recipe-applied:${recipeId}:${key}`;
}

/** Leading-whitespace (indentation) of the given line. */
function indentOf(line: string): string {
	const m = line.match(/^[\t ]*/);
	return m ? m[0] : '';
}

/**
 * Detect the platform line ending used by `contents` so inserted text matches.
 * Defaults to `\n`.
 */
function eolOf(contents: string): string {
	return contents.includes('\r\n') ? '\r\n' : '\n';
}

/**
 * Whether this splice has already been applied to `contents` (sentinel present).
 */
export function isSpliceApplied(contents: string, recipeId: string, splice: Splice): boolean {
	return contents.includes(sentinelFor(recipeId, splice.key));
}

/**
 * Apply one splice to `contents`, returning the new contents.
 *
 * Idempotent: if the sentinel is already present, `contents` is returned
 * unchanged. Fail-closed: if the anchor is missing, throws {@link RecipeError}
 * (the caller must abort the whole plan before writing anything).
 *
 * The payload is re-indented to the anchor's indentation and a sentinel comment
 * line is appended so future applies detect it.
 */
export function applySplice(contents: string, recipeId: string, splice: Splice): string {
	if (isSpliceApplied(contents, recipeId, splice)) return contents;

	const eol = eolOf(contents);
	const lines = contents.split(/\r?\n/);
	const anchorIndex = lines.findIndex((l) => l.includes(splice.anchor));
	if (anchorIndex === -1) {
		throw new RecipeError(
			`recipe "${recipeId}": splice anchor ${JSON.stringify(splice.anchor)} not found in ` +
				`${splice.file}. The base template must ship this anchor marker.`
		);
	}

	const indent = indentOf(lines[anchorIndex]);
	const sentinel = sentinelFor(recipeId, splice.key);
	const block = [
		...splice.payload.split(/\r?\n/).map((l) => (l.length ? indent + l : l)),
		indent + sentinel
	];

	const position = splice.position ?? 'after';
	const insertAt = position === 'after' ? anchorIndex + 1 : anchorIndex;
	lines.splice(insertAt, 0, ...block);
	return lines.join(eol);
}

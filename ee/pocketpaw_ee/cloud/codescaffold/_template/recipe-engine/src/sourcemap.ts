import { promises as fs } from 'node:fs';
import path from 'node:path';
import type { SourceMap } from './types.js';

/**
 * Path segments never included when reading a project into a source map:
 * dependencies, build output, and generated files. These are reproduced by
 * `pnpm install` / `pnpm build` and must not travel in the source map.
 */
const DEFAULT_IGNORE = new Set([
	'node_modules',
	'.svelte-kit',
	'.wrangler',
	'.output',
	'.vercel',
	'.netlify',
	'build',
	'.git',
	'.DS_Store',
	'pnpm-lock.yaml',
	'worker-configuration.d.ts'
]);

function toPosix(p: string): string {
	return p.split(path.sep).join('/');
}

/** Recursively read a directory into a `{ path: contents }` source map. */
export async function readSourceMap(
	dir: string,
	ignore: Set<string> = DEFAULT_IGNORE
): Promise<SourceMap> {
	const map: SourceMap = {};

	async function walk(current: string): Promise<void> {
		const entries = await fs.readdir(current, { withFileTypes: true });
		for (const entry of entries) {
			if (ignore.has(entry.name)) continue;
			const abs = path.join(current, entry.name);
			if (entry.isDirectory()) {
				await walk(abs);
			} else if (entry.isFile()) {
				const rel = toPosix(path.relative(dir, abs));
				map[rel] = await fs.readFile(abs, 'utf8');
			}
		}
	}

	await walk(dir);
	return map;
}

/**
 * Write a source map to `dir`, creating parent directories as needed. Existing
 * files not present in the map are left untouched (additive write).
 */
export async function writeSourceMap(dir: string, map: SourceMap): Promise<void> {
	for (const [rel, contents] of Object.entries(map)) {
		const abs = path.join(dir, ...rel.split('/'));
		await fs.mkdir(path.dirname(abs), { recursive: true });
		await fs.writeFile(abs, contents, 'utf8');
	}
}

/** A stable, diff-friendly digest of a source map (sorted `path\tlen` lines). */
export function digest(map: SourceMap): string {
	return Object.keys(map)
		.sort()
		.map((k) => `${k}\t${map[k].length}`)
		.join('\n');
}

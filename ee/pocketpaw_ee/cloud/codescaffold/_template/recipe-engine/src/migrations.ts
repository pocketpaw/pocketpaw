import type { SourceMap } from './types.js';

const MIGRATION_RE = /^migrations\/(\d{4})_(.+)\.sql$/;

/** All migration paths in the source map, sorted by numeric prefix ascending. */
export function listMigrations(map: SourceMap): Array<{ path: string; num: number; name: string }> {
	return Object.keys(map)
		.map((path) => {
			const m = path.match(MIGRATION_RE);
			return m ? { path, num: Number(m[1]), name: m[2] } : null;
		})
		.filter((x): x is { path: string; num: number; name: string } => x !== null)
		.sort((a, b) => a.num - b.num);
}

/** The existing migration for `name`, if one is already present (idempotency). */
export function findMigrationByName(
	map: SourceMap,
	name: string
): { path: string; num: number; name: string } | undefined {
	return listMigrations(map).find((m) => m.name === name);
}

/** The next migration number = highest existing prefix + 1 (base ships 0001). */
export function nextMigrationNumber(map: SourceMap): number {
	const migrations = listMigrations(map);
	const max = migrations.reduce((acc, m) => Math.max(acc, m.num), 0);
	return max + 1;
}

/** Zero-padded 4-digit migration filename for a given number + name. */
export function migrationPath(num: number, name: string): string {
	return `migrations/${String(num).padStart(4, '0')}_${name}.sql`;
}

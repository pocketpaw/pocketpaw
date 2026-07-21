import type { Recipe } from '../../recipe-engine/src/types';

/**
 * `db` — the database capability.
 *
 * The base template already ships the full Drizzle + Cloudflare D1 wiring:
 *   - `src/lib/server/db/index.ts`   — `createDb(platform.env.DB)` factory (drizzle-orm/d1)
 *   - `src/lib/server/db/schema.ts`  — the schema barrel with the `@recipe:schema` anchor
 *   - `drizzle.config.ts`            — generate-only config emitting `migrations/NNNN_*.sql`
 *   - `migrations/0001_init.sql`     — the baseline migration
 *
 * so the template builds and deploys standalone. This recipe is therefore the
 * trivial proof of the composition model: it declares the `database` capability
 * and adds nothing. Other recipes depend on it via `requires: ['db']`, which
 * exercises deterministic dependency resolution.
 */
export const recipe: Recipe = {
	id: 'db',
	capability: 'database'
};

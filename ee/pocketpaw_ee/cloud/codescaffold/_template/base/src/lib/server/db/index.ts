import { drizzle } from 'drizzle-orm/d1';
import * as schema from './schema';

export { schema };

/**
 * Create a Drizzle client bound to the per-request Cloudflare D1 database.
 *
 * D1 is provisioned per-site by the platform and bound to the Worker as `env.DB`.
 * Always read it through `platform.env.DB` inside a server context
 * (`+page.server.ts`, `+server.ts`, `hooks.server.ts`) — never a connection string.
 *
 *     export const load = async ({ platform }) => {
 *       const db = createDb(platform!.env.DB);
 *       // ...
 *     };
 */
export function createDb(DB: D1Database) {
	return drizzle(DB, { schema });
}

export type Database = ReturnType<typeof createDb>;

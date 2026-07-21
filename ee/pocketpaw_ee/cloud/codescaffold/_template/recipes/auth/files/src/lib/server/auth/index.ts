import { betterAuth } from 'better-auth';
import { drizzleAdapter } from 'better-auth/adapters/drizzle';
import { sveltekitCookies } from 'better-auth/svelte-kit';
import { getRequestEvent } from '$app/server';
import { drizzle } from 'drizzle-orm/d1';
import * as schema from './schema';

/**
 * Create a per-request Better Auth instance bound to the site's Cloudflare D1
 * database. Both the D1 binding and the signing secret come from the Worker via
 * `platform.env` — the secret is a Cloudflare Worker secret, never committed.
 *
 * An instance is created per request (not a module singleton) because the D1
 * binding only exists at request time via `platform.env.DB`.
 */
export function createAuth(env: App.Platform['env']) {
	const db = drizzle(env.DB, { schema });
	return betterAuth({
		secret: env.AUTH_SECRET,
		database: drizzleAdapter(db, { provider: 'sqlite', schema }),
		emailAndPassword: { enabled: true },
		// Sets auth cookies on the active SvelteKit request (server actions/endpoints).
		plugins: [sveltekitCookies(getRequestEvent)]
	});
}

export type Auth = ReturnType<typeof createAuth>;

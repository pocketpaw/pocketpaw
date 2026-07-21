// See https://svelte.dev/docs/kit/types#app.d.ts
// for information about these interfaces
declare global {
	namespace App {
		interface Locals {
			// Recipes add per-request locals here (e.g. the auth session/user).
			// @recipe:locals
		}

		interface Platform {
			// `Env` is generated from wrangler.jsonc by `wrangler types` and already
			// carries the D1 `DB` binding. Recipes append Worker secret/var typings
			// at the anchor below. Secret *values* are provisioned out of band as
			// Cloudflare Worker secrets — only their names/types ever live in source.
			env: Env & {
				// @recipe:env
			};
			ctx: ExecutionContext;
			caches: CacheStorage;
			cf?: IncomingRequestCfProperties;
		}

		// interface Error {}
		// interface PageData {}
		// interface PageState {}
	}
}

export {};

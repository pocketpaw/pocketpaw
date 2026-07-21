import type { Recipe } from '../../recipe-engine/src/types';

/**
 * `auth` — email/password authentication with Better Auth on Cloudflare D1.
 *
 * Adds a per-request Better Auth instance bound to `platform.env.DB`, session
 * loading via a `hooks.server.ts` handle, sign-in/sign-up routes and a protected
 * dashboard (shadcn-svelte + Valibot), the Better Auth Drizzle tables (stacked
 * migration), and an `AUTH_SECRET` Worker-secret declaration.
 *
 * The signing secret is a Cloudflare Worker secret provisioned out of band — the
 * recipe declares only its NAME; no secret value ever enters the source map.
 */
export const recipe: Recipe = {
	id: 'auth',
	capability: 'authentication',
	requires: ['db'],

	files: [
		{ from: 'files/src/lib/server/auth/index.ts', to: 'src/lib/server/auth/index.ts' },
		{ from: 'files/src/lib/server/auth/schema.ts', to: 'src/lib/server/auth/schema.ts' },
		{ from: 'files/src/lib/server/auth/hooks.ts', to: 'src/lib/server/auth/hooks.ts' },
		{ from: 'files/src/lib/server/auth/types.ts', to: 'src/lib/server/auth/types.ts' },
		{ from: 'files/src/lib/client/auth.ts', to: 'src/lib/client/auth.ts' },
		{ from: 'files/src/lib/validations/auth.ts', to: 'src/lib/validations/auth.ts' },
		{ from: 'files/src/routes/sign-in/+page.server.ts', to: 'src/routes/sign-in/+page.server.ts' },
		{ from: 'files/src/routes/sign-in/+page.svelte', to: 'src/routes/sign-in/+page.svelte' },
		{ from: 'files/src/routes/sign-up/+page.server.ts', to: 'src/routes/sign-up/+page.server.ts' },
		{ from: 'files/src/routes/sign-up/+page.svelte', to: 'src/routes/sign-up/+page.svelte' },
		{ from: 'files/src/routes/dashboard/+page.server.ts', to: 'src/routes/dashboard/+page.server.ts' },
		{ from: 'files/src/routes/dashboard/+page.svelte', to: 'src/routes/dashboard/+page.svelte' }
	],

	splices: [
		{
			file: 'src/hooks.server.ts',
			anchor: '// @recipe:imports',
			key: 'hooks-import',
			payload: `import { authHandle } from '$lib/server/auth/hooks';`
		},
		{
			file: 'src/hooks.server.ts',
			anchor: '// @recipe:handlers',
			key: 'hooks-handler',
			payload: `authHandle,`
		},
		{
			file: 'src/lib/server/db/schema.ts',
			anchor: '// @recipe:schema',
			key: 'schema',
			payload: `export * from '../auth/schema';`
		},
		{
			file: 'src/lib/components/nav.svelte',
			anchor: '// @recipe:nav',
			key: 'nav',
			payload: `{ href: '/dashboard', label: 'Dashboard' },\n{ href: '/sign-in', label: 'Sign in' },`
		}
	],

	migration: { name: 'auth', from: 'files/migration.sql' },

	secrets: ['AUTH_SECRET'],
	deps: {
		'better-auth': '^1.6.23',
		valibot: '^1.4.2'
	},
	envTypes: ['AUTH_SECRET: string;'],
	locals: [
		"session: import('$lib/server/auth/types').Session | null;",
		"user: import('$lib/server/auth/types').User | null;"
	]
};

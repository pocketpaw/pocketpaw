import type { Recipe } from '../../recipe-engine/src/types';

/**
 * `stripe` — payments via Stripe Checkout on Cloudflare Workers + D1.
 *
 * Adds a checkout endpoint that creates a Stripe Checkout Session, a webhook
 * endpoint that verifies the signature with the Web Crypto async verifier
 * (mandatory) and records paid orders in D1, an `order` table (stacked
 * migration), and `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` Worker-secret
 * declarations.
 *
 * Requires the `db` capability. Stripe keys are Cloudflare Worker secrets
 * provisioned out of band — the recipe declares only their NAMES; no key value
 * ever enters the source map or the world-readable asset tree.
 */
export const recipe: Recipe = {
	id: 'stripe',
	capability: 'payments',
	requires: ['db'],

	files: [
		{ from: 'files/src/lib/server/payments/stripe.ts', to: 'src/lib/server/payments/stripe.ts' },
		{ from: 'files/src/lib/server/payments/schema.ts', to: 'src/lib/server/payments/schema.ts' },
		{ from: 'files/src/routes/api/checkout/+server.ts', to: 'src/routes/api/checkout/+server.ts' },
		{ from: 'files/src/routes/api/stripe/webhook/+server.ts', to: 'src/routes/api/stripe/webhook/+server.ts' },
		{ from: 'files/src/routes/checkout/+page.svelte', to: 'src/routes/checkout/+page.svelte' },
		{ from: 'files/src/routes/checkout/success/+page.svelte', to: 'src/routes/checkout/success/+page.svelte' }
	],

	splices: [
		{
			file: 'src/lib/server/db/schema.ts',
			anchor: '// @recipe:schema',
			key: 'schema',
			payload: `export * from '../payments/schema';`
		},
		{
			file: 'src/lib/components/nav.svelte',
			anchor: '// @recipe:nav',
			key: 'nav',
			payload: `{ href: '/checkout', label: 'Checkout' },`
		}
	],

	migration: { name: 'orders', from: 'files/migration.sql' },

	secrets: ['STRIPE_SECRET_KEY', 'STRIPE_WEBHOOK_SECRET'],
	deps: {
		stripe: '^22.3.1'
	},
	envTypes: ['STRIPE_SECRET_KEY: string;', 'STRIPE_WEBHOOK_SECRET: string;']
};

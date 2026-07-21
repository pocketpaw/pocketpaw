import Stripe from 'stripe';

/**
 * Create a Stripe client configured for the Cloudflare Workers runtime.
 *
 * Workers has no Node `http` module, so Stripe must use the Fetch HTTP client.
 * The secret key is a Cloudflare Worker secret read from `platform.env` — it is
 * server-only and never reaches the client bundle or the asset tree.
 */
export function createStripe(env: App.Platform['env']) {
	return new Stripe(env.STRIPE_SECRET_KEY, {
		httpClient: Stripe.createFetchHttpClient()
	});
}

/**
 * Web Crypto provider for verifying webhook signatures at the edge. WebCrypto is
 * async, so webhook verification must use `constructEventAsync` with this
 * provider (the sync `constructEvent` does not work on Workers).
 */
export const stripeCryptoProvider = Stripe.createSubtleCryptoProvider();

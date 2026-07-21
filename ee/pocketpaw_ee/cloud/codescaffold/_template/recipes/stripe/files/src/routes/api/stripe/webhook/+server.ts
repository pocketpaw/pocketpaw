import { error, text } from '@sveltejs/kit';
import type Stripe from 'stripe';
import { createStripe, stripeCryptoProvider } from '$lib/server/payments/stripe';
import { createDb } from '$lib/server/db';
import { order } from '$lib/server/payments/schema';
import type { RequestHandler } from './$types';

/**
 * Stripe webhook receiver. Verifies the signature against the
 * `STRIPE_WEBHOOK_SECRET` Worker secret (mandatory) using the async Web Crypto
 * verifier, then records completed checkout sessions in D1. Signature
 * verification must happen on the RAW request body.
 */
export const POST: RequestHandler = async (event) => {
	const signature = event.request.headers.get('stripe-signature');
	if (!signature) error(400, 'Missing stripe-signature header');

	const payload = await event.request.text();
	const stripe = createStripe(event.platform!.env);

	let stripeEvent: Stripe.Event;
	try {
		stripeEvent = await stripe.webhooks.constructEventAsync(
			payload,
			signature,
			event.platform!.env.STRIPE_WEBHOOK_SECRET,
			undefined,
			stripeCryptoProvider
		);
	} catch (err) {
		error(400, `Webhook signature verification failed: ${(err as Error).message}`);
	}

	if (stripeEvent.type === 'checkout.session.completed') {
		const session = stripeEvent.data.object;
		const db = createDb(event.platform!.env.DB);
		await db
			.insert(order)
			.values({
				stripeSessionId: session.id,
				stripeCustomerId: typeof session.customer === 'string' ? session.customer : null,
				email: session.customer_details?.email ?? null,
				amountTotal: session.amount_total ?? null,
				currency: session.currency ?? null,
				status: 'paid'
			})
			.onConflictDoNothing();
	}

	return text('ok');
};

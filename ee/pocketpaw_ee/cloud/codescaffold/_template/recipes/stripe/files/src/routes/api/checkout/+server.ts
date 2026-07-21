import { json } from '@sveltejs/kit';
import { createStripe } from '$lib/server/payments/stripe';
import type { RequestHandler } from './$types';

/**
 * Create a Stripe Checkout Session and return its hosted-checkout URL.
 * Demo uses a single inline line item — replace with your own catalog/pricing.
 */
export const POST: RequestHandler = async (event) => {
	const stripe = createStripe(event.platform!.env);
	const origin = event.url.origin;

	const session = await stripe.checkout.sessions.create({
		mode: 'payment',
		line_items: [
			{
				price_data: {
					currency: 'usd',
					product_data: { name: 'Demo product' },
					unit_amount: 1000
				},
				quantity: 1
			}
		],
		success_url: `${origin}/checkout/success?session_id={CHECKOUT_SESSION_ID}`,
		cancel_url: `${origin}/checkout`
	});

	return json({ url: session.url });
};

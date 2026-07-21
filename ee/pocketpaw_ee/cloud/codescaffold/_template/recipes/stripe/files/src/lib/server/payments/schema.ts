import { sqliteTable, text, integer } from 'drizzle-orm/sqlite-core';

/**
 * Orders recorded from completed Stripe Checkout sessions. Written by the
 * webhook handler on `checkout.session.completed`. The Stripe session id is
 * unique so webhook retries are idempotent.
 */
export const order = sqliteTable('order', {
	id: text('id')
		.primaryKey()
		.$defaultFn(() => crypto.randomUUID()),
	stripeSessionId: text('stripe_session_id').notNull().unique(),
	stripeCustomerId: text('stripe_customer_id'),
	email: text('email'),
	amountTotal: integer('amount_total'),
	currency: text('currency'),
	status: text('status').notNull().default('pending'),
	createdAt: integer('created_at', { mode: 'timestamp_ms' })
		.$defaultFn(() => new Date())
		.notNull()
});

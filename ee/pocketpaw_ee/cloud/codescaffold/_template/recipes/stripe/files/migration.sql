-- Orders table for Stripe Checkout, on Cloudflare D1 (SQLite).
-- Generated from the Drizzle schema; DDL made idempotent (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS `order` (
	`id` text PRIMARY KEY NOT NULL,
	`stripe_session_id` text NOT NULL,
	`stripe_customer_id` text,
	`email` text,
	`amount_total` integer,
	`currency` text,
	`status` text DEFAULT 'pending' NOT NULL,
	`created_at` integer NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS `order_stripe_session_id_unique` ON `order` (`stripe_session_id`);

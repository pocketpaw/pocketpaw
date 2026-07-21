import { defineConfig } from 'drizzle-kit';

// Cloudflare D1 uses SQLite. We only use drizzle-kit to *generate* SQL migration
// files into `migrations/` (wrangler's default migrations_dir); wrangler applies
// them to D1 (`wrangler d1 migrations apply`). Generation reads the local schema
// only and never contacts Cloudflare, so no account credentials are needed here.
export default defineConfig({
	schema: './src/lib/server/db/schema.ts',
	out: './migrations',
	dialect: 'sqlite',
	verbose: true,
	strict: true
});

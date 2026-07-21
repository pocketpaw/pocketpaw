// Barrel of all Drizzle table definitions for Cloudflare D1.
//
// The base template ships no tables of its own. Each feature recipe adds a
// self-contained schema module under `./schema/<recipe>.ts` (with its own
// `drizzle-orm/sqlite-core` imports) and re-exports it at the anchor below.
// `createDb()` imports this barrel as `* as schema`, so every re-exported table
// is registered with Drizzle. Keep the anchor as the LAST re-export line.
//
// @recipe:schema

export {};

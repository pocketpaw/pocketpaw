-- 0001_init — base template initial migration.
--
-- The base template ships no tables of its own. Feature recipes add numbered
-- migrations that stack on top of this baseline (auth -> 0002, stripe -> 0003, …),
-- each using idempotent DDL (CREATE TABLE IF NOT EXISTS ...).
--
-- This no-op statement gives `wrangler d1 migrations apply` a baseline entry.
SELECT 1;

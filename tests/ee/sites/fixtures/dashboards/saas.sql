-- Eval schema: a SaaS app on better-auth (sign-in tables) with subscriptions and
-- epoch-millisecond product events.
CREATE TABLE "user" (id TEXT PRIMARY KEY, name TEXT, email TEXT, "emailVerified" INTEGER,
  "createdAt" TEXT);
CREATE TABLE session (id TEXT PRIMARY KEY, token TEXT, "userId" TEXT REFERENCES "user"(id),
  "expiresAt" TEXT);
CREATE TABLE account (id TEXT PRIMARY KEY, "userId" TEXT REFERENCES "user"(id), password TEXT);
CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, user_id TEXT REFERENCES "user"(id),
  plan TEXT, mrr_cents INTEGER, status TEXT, started_at TEXT);
CREATE TABLE events (id INTEGER PRIMARY KEY, user_id TEXT, kind TEXT, at INTEGER);
INSERT INTO "user" VALUES ('u1', 'Ann', 'ann@x.io', 1, '2026-10-01'), ('u2', 'Ben', 'b@x.io', 0,
  '2026-10-02');
INSERT INTO session VALUES ('s1', 'tok-secret', 'u1', '2027-01-01');
INSERT INTO account VALUES ('a1', 'u1', 'scrypt$hash');
INSERT INTO subscriptions VALUES (1, 'u1', 'pro', 2900, 'active', '2026-10-01'),
  (2, 'u2', 'free', 0, 'active', '2026-10-02');
INSERT INTO events VALUES (1, 'u1', 'signup', 1790000000000), (2, 'u1', 'activated', 1790000500000),
  (3, 'u2', 'signup', 1790090000000);

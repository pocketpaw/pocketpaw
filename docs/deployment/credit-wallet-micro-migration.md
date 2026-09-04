# Converting the credit wallet to micro-credits

A one-time database change that has to happen while the API and worker are
stopped. It is not optional and it is not automatic: nothing in the image or the
compose file runs it.

## Why it exists

A credit is a cent, and the proxy prices a single API call. A $0.0015 call is
0.375 of a credit, and an integer wallet has no honest number for that. Amounts
are now stored in millionths of a credit instead, so the three fields that carry
money were renamed:

| collection | before | after |
|---|---|---|
| `credit_balances` | `balance_credits` | `balance_micro` |
| `credit_ledger` | `amount_delta` | `amount_delta_micro` |
| `credit_ledger` | `balance_after` | `balance_after_micro` |

Customers see no change. Balances, top-ups, plans and prices are still whole
credits, converted at the HTTP boundary. One credit is still one cent.

## Running it

```bash
# 1. Stop the writers. Both of them.
docker compose stop api worker

# 2. See what would change. Writes nothing.
python scripts/migrations/2026_09_04_micro_credits.py --dry-run

# 3. Convert. Exits non-zero and says so if anything is left over.
python scripts/migrations/2026_09_04_micro_credits.py

# 4. Start again.
docker compose start api worker
```

It reads the same `POCKETPAW_MONGO_URL` and `POCKETPAW_MONGO_DB` the app uses.
Running it twice is safe: documents are selected by the old field's existence, so
the second run matches nothing and reports zero.

## What happens if you skip it

This shipped to production on 2026-09-04 without being run, so the failure mode is
measured rather than predicted. Three of its four symptoms are silent.

- **Every balance reads zero.** `balance_micro` has a default of 0, so a row that
  has never held the field parses as an empty wallet. Nothing logs anything.
- **Paying customers are locked out.** The run-start gate and every strict debit
  read that zero and refuse with "insufficient credits".
- **The ledger endpoint 500s.** `amount_delta_micro` is required with no default,
  so an old row fails to parse. This is the only audible symptom, and it is the
  one that surfaced the incident.
- **Grants land beside the old value.** A top-up bought while the app is in this
  state increments the new field on a document that still carries the old one.

The boot now refuses rather than allowing any of that:

```
cloud startup: the credit wallet is still in whole credits — credit_balances.balance_credits
present. This build reads micro-credits, and an unconverted balance row reads as an EMPTY
wallet rather than failing, so serving it would tell paying customers they have no credits.
Refusing to start. Stop the API and worker, run `python scripts/migrations/2026_09_04_micro_credits.py`
(--dry-run first), then start again.
```

If you see that, the container is doing its job. Run the migration.

## Recovering a database the app already served

A deployment that ran unmigrated for a while has balance documents carrying both
fields: the old balance, plus whatever grants and metered debits were applied
since. The migration adds the two together rather than overwriting, so those
movements survive — run it normally, no special flag. Verify a known workspace
afterwards:

```bash
python - <<'EOF'
import asyncio, os
from motor.motor_asyncio import AsyncIOMotorClient
async def main():
    db = AsyncIOMotorClient(os.environ["POCKETPAW_MONGO_URL"])[os.environ.get("POCKETPAW_MONGO_DB", "pocketpaw")]
    async for d in db["credit_balances"].find({}, {"workspace": 1, "balance_micro": 1}):
        print(d["workspace"], d.get("balance_micro", 0) / 1_000_000, "credits")
asyncio.run(main())
EOF
```

Cross-check any workspace that looks wrong against its ledger, which is the
authority: `credits.service.reconcile(workspace)` recomputes the balance from the
entries whose effect actually landed and repairs the row.

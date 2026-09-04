# Converting the credit wallet to micro-credits

A one-time database change. Nothing in the image or the compose file runs it, and
the app will not start until it has been done.

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

## Running it on Coolify

The migration ships inside the installed package, so it runs anywhere the app
runs. That is deliberate: it started life in `scripts/`, which the runtime image
does not carry. The runtime stage of `deploy/coolify/Dockerfile` copies
`/opt/venv` and `/build/connectors` and nothing else, so the repository exists
only in a discarded build layer, and on Coolify there is no checkout on the host
either.

Open a terminal on the `paw-backend` container from the Coolify dashboard, or SSH
to the host and use `docker exec`:

```bash
docker exec -it paw-backend \
  python -m pocketpaw_ee.cloud.credits.migrate_micro_credits --dry-run

docker exec -it paw-backend \
  python -m pocketpaw_ee.cloud.credits.migrate_micro_credits
```

It reads `CLOUD_MONGODB_URI` from the container's own environment, which is
already set to `mongodb://mongo:27017/paw-enterprise`, and takes the database name
off that URI exactly as `init_cloud_db` does. There is nothing to configure.

**You do not need to stop the writers.** The conversion is one atomic update per
document and it adds to the destination field rather than replacing it, so an
increment arriving mid-run lands correctly whether it gets there before or after.
Nothing calls `credits.service.reconcile` automatically, which is the one routine
that could have rewritten a balance from a half-converted ledger.

Stopping is still the tidier option if you have a maintenance window, because
reads served during the run are wrong. It is not a correctness requirement.

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
Refusing to start. Run `python -m pocketpaw_ee.cloud.credits.migrate_micro_credits --dry-run`
to see what it would convert, then `python -m pocketpaw_ee.cloud.credits.migrate_micro_credits`
to convert it.
```

A container in that state restarts in a loop, so run the migration from the `mongo`
container's neighbour on the host rather than waiting for a terminal on a backend
that keeps dying. If the backend is already up, run it there and redeploy after.

## Recovering a database the app already served

A deployment that ran unmigrated for a while has balance documents carrying both
fields: the old balance, plus whatever grants and metered debits were applied
since. The migration adds the two together rather than overwriting, so those
movements survive. Run it normally, with no special flag, then spot-check:

```bash
docker exec -i paw-mongo mongosh --quiet paw-enterprise --eval '
  db.credit_balances.find({}, {workspace: 1, balance_micro: 1}).forEach(d =>
    print(d.workspace, d.balance_micro / 1000000, "credits"))'
```

Any workspace that looks wrong can be settled against the ledger, which is the
authority: `credits.service.reconcile(workspace)` recomputes the balance from the
entries whose effect actually landed and repairs the row.

## Pointing it at the wrong database

The migration selects documents by the old field's existence, so a run against the
wrong database converts nothing and prints `0 document(s) would convert` — which
is character for character what an already-migrated database prints. It refuses to
run at all when `credit_balances` and `credit_ledger` are absent, for that reason.
If you see that refusal, check `CLOUD_MONGODB_URI` rather than the data.

# Converting the credit wallet to micro-credits

A one-time database change, run automatically as a deploy step. The app will not
start until it has been done.

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

## How it runs

`deploy/coolify/docker-compose.yaml` has a `migrate` service: the same image as the
backend, `restart: "no"`, running the migration and exiting. The backend declares
`depends_on: migrate: service_completed_successfully`, so a deploy converts the
database and only then starts the app. A failed migration holds the backend back
rather than letting it up against half-converted data.

Deploying is the whole procedure. There is nothing to run by hand, which is the
point: the runtime image carries only `/opt/venv`, so `scripts/` does not exist in
it, and Coolify keeps no checkout on the host and may offer no shell at all. A
migration has to arrive as part of a deploy or it cannot arrive.

Watch it in the Coolify logs for the `migrate` service:

```
INFO micro-credit migration — host mongo:27017, database paw-enterprise — WRITING
INFO credit_balances: 12 document(s) converted balance_credits -> balance_micro (x1000000)
INFO migration complete and verified — 12 credit document(s) converted
```

Running on every deploy is safe. Documents are selected by the old field's
existence, so the second deploy matches nothing and reports zero.

### Running it by hand

Only if you have a shell, and only for a dry run or an out-of-band repair. The
Coolify dashboard has a Terminal tab on the application; `docker exec` works over
SSH.

```bash
docker exec -it paw-backend   python -m pocketpaw_ee.cloud.credits.migrate_micro_credits --dry-run
```

It reads `CLOUD_MONGODB_URI` from the container's own environment, so there is
nothing to configure.

### Adding another migration later

Append it to the `migrate` service's command, oldest first, `&&`-chained so a
failure stops the rest. Every migration on that list must be idempotent, because it
runs on every deploy. Delete a line once no reachable deployment predates it.

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

## Recovering a database the app already served

A deployment that ran unmigrated for a while has balance documents carrying both
fields: the old balance, plus whatever grants and metered debits were applied
since. The migration adds the two together rather than overwriting, so those
movements survive. The ordinary deploy handles it; spot-check afterwards:

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

A completely empty database is the exception and passes silently. That is a first
deploy, where the app has not yet created its collections and there is nothing to
convert. It has to pass: the backend waits on this step succeeding, so refusing an
empty database would hold a brand new install down forever.

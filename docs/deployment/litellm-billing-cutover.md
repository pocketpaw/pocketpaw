# Cutting billing over to LiteLLM

How to move from per-run metering to LiteLLM as the single meter, without
double-billing anyone or handing out free usage.

There are three modes, set by `POCKETPAW_LITELLM_SPEND_MODE`:

| mode | who charges | debits |
|---|---|---|
| `off` (default) | per-run metering prices each run locally | yes |
| `shadow` | per-run metering still charges; LiteLLM spend is read and compared | no, the compare debits nothing |
| `live` | LiteLLM only; the per-run sweep returns 0 without charging | yes, from proxy spend |

Both sweeps run on the same five minute heartbeat.

## Before anything: the proxy has to record who a request was for

Spend is attributed to a workspace by the `user` field each request carries, which
the proxy stores on the spend row as its `end_user`. Every chat request sets it.
Nothing else does, and nothing else can: chat authenticates with the deployment's
own key, so a read filtered by a tenant's virtual key cannot see a chat run at all.

Two things on the proxy have to be true, and neither is visible from our side.

**End-user cost tracking must be on.** `disable_end_user_cost_tracking` drops the
id on the way to the spend row. It does not error; the rows simply arrive
anonymous, and everything downstream reports zero.

**The proxy must serve `/spend/logs/v2`.** That is the only spend route that
filters on `end_user`. On a proxy too old to have it, each sweep logs a failed
customer read and falls back to the per-key read, which bills media and Studio and
nothing else.

Check both before you go further:

```bash
curl -s -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  "$LITELLM_BASE/spend/logs/v2?start_date=$(date -u -d yesterday +%F)&end_date=$(date -u +%F)&page_size=5" \
  | head -c 400
```

Rows should come back with a populated `end_user`. Empty ids on rows you know came
from chat mean the tracking switch, not the endpoint.

## Do not flip straight to `live`

Three things go wrong if you do, and none of them announces itself.

**The whole history gets billed at once.** Spend ingestion skips rows older than
a per-tenant high-water mark, and that mark is empty until something ingests. On
the first live sweep the guard is bypassed and every row the proxy returns is
charged. Those rows include every chat run the per-run meter already billed, under
a different idempotency key, so the ledger's uniqueness index cannot recognise
them as duplicates. There is no run id on the proxy key's metadata to match
against either, which is why row-level de-duplication does not exist.

**Unprovisioned workspaces stop being billed at all.** The cutover sweep iterates
only tenants that hold a proxy key. In `live` the per-run sweep is off for
everyone, so a workspace without a key is charged by nothing. Its usage is free
and silently so.

**Anything the proxy cannot attribute is free.** This is the one that bit us. For
as long as `live` was on, chat was billed to nobody: the ingest read spend by
virtual key, chat sends the deployment key, and the two never met. The sweep
reported `ingested spend for 3/3 tenants -> 0 credits` the whole time and no part
of it was lying. Every tick now also counts the window's spend rows that no swept
workspace claims, and says so:

```
spend_attribution_coverage: [...] 41 of 128 proxy spend row(s) belong to NO swept
workspace — that spend is being served and not billed
```

Treat a non-zero count as blocking. It is the number that tells you whether the
meter you are about to trust alone can see what people are actually spending.

## The procedure

**1. Run `shadow` for a while.** It reads proxy spend and the ledger over the same
window and records a reconciliation row per tenant with the delta and a
`coverage_gap` verdict. It moves no money. This is how you find out whether the
two meters agree before you trust one of them alone.

Shadow also runs the attribution-coverage check, so this is where you see whether
anything is falling through before it costs you. Watch two numbers, not one: the
per-tenant `delta`, and `unattributed`.

```python
from pocketpaw_ee.cloud.llm_provisioning.cutover_sweeper import run_cutover_sweep
await run_cutover_sweep(mode="shadow")
# {'tenants': 3, 'processed': 3, 'gaps': 0, 'credits': 0, 'unattributed': 0}
```

`unattributed` must be zero, and a zero you got while a count was failing does not
count — the log line says `INCOMPLETE` in that case rather than reporting a
finding.

**2. Drain the per-run sweep.** Let it run until no completed run is still
unbilled. A run left unbilled at the moment you flip is billed by neither meter:
the per-run sweep no-ops in `live`, and its proxy rows predate the cutover mark
so ingestion skips them.

**3. Check who is provisioned.**

```python
from pocketpaw_ee.cloud.llm_provisioning import service as provisioning
await provisioning.list_provisioned_workspaces()
```

Compare that against the workspaces you expect to bill. Provision any that are
missing, or accept that they will be free.

**4. Stamp the cutover mark, dry run first.**

```python
report = await provisioning.prepare_spend_cutover(dry_run=True)
# CutoverPreparation(cutover_at=..., provisioned=N, seeded=N, already_marked=0, dry_run=True)
```

`seeded` is how many tenants would get a mark. `already_marked` are tenants that
have already ingested; those are left alone, because moving a live mark forward
would drop the spend between the old mark and now.

When the numbers look right, run it for real:

```python
report = await provisioning.prepare_spend_cutover()
```

That instant is the seam. Per-run metering owns every run before it; LiteLLM owns
every proxy row after it.

**5. Set the mode.**

```
POCKETPAW_LITELLM_SPEND_MODE=live
```

## What changes for customers

**Everything routed through the proxy is billed, not just chat.** Per-run metering
charged one row per chat run. Proxy spend includes every call the proxy served,
so embeddings, title generation and any other internal traffic now reach the
wallet. That is real cost and arguably belongs there, but it is a change in what
people pay and it is worth knowing before support asks.

**The rate card is unchanged.** Live mode reuses the same `billing_markup` and
`credit_usd`, so the conversion is still `round(cost * markup / credit_usd)` and
charges are still whole credits worth a cent each. A run costing less than a cent
still rounds to one.

**Chat is billed at all, which it was not before.** If you ran `live` before the
attribution fix, chat was free for everyone. Turning this on is not a price rise
in the rate-card sense, but it will look like one on the first invoice after it,
and it is the change support will hear about.

## A note on trusting the `user` field

LiteLLM's own documentation warns that a caller can declare any `user` it likes,
and the proxy will attribute the cost to whatever it says. That would be a way for
one tenant to charge another.

It does not reach us, because the field is not ours to forward: both agent backends
set it from the run's own bound identity at the point the model is built, and no
part of a customer's request body is passed through to the proxy. The warning
becomes ours the moment either of those stops being true — if a tenant is ever
handed a proxy key to call directly, or a request body is ever proxied verbatim,
the id has to move to a header set server-side.

## Rolling back

Set the mode back to `off`. Per-run metering resumes for runs going forward.

Runs that stayed unbilled while `live` was in effect are **not** back-billed. The
cutover mark also stays where it is, so a later return to `live` picks up from
that mark rather than replaying history, which is the behaviour you want.

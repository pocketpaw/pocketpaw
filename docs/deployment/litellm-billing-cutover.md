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

## Do not flip straight to `live`

Two things go wrong if you do, and neither announces itself.

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

## The procedure

**1. Run `shadow` for a while.** It reads proxy spend and the ledger over the same
window and records a reconciliation row per tenant with the delta and a
`coverage_gap` verdict. It moves no money. This is how you find out whether the
two meters agree before you trust one of them alone.

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

## Rolling back

Set the mode back to `off`. Per-run metering resumes for runs going forward.

Runs that stayed unbilled while `live` was in effect are **not** back-billed. The
cutover mark also stays where it is, so a later return to `live` picks up from
that mark rather than replaying history, which is the behaviour you want.

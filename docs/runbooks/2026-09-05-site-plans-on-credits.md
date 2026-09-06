# Paw Sites plans are paid from workspace credits — Dodo is out

Created 2026-09-05 (`fix/sites-plan-credits`). Read this before touching site
billing, before diagnosing a site stuck showing "pending", and before assuming
any site charge appears on a Dodo invoice.

## What was broken

Selecting a paid plan for a site produced a site that said **pending payment** and
never went live. There was no payment to complete.

The publish path had two Dodo rails and picked the wrong one for the deployment it
was running on. A paid tier with an add-on id configured took the add-on rail,
which attaches the site as a line on the workspace's **own** Dodo subscription.
That rail refuses a workspace with no subscription — `NoActiveSubscription` — and
that refusal was not a bug in it, it was its documented shape: an add-on has to
attach to something.

What the refusal did to the site is the bug. The site was created PENDING first,
so the failed charge left a real `Site` document sitting undeployed, and the
Billing tab rendered the copy for the *other* rail: "Pending payment — complete
checkout to publish." No checkout had been opened. Nothing in the product could
open one. There was no action the operator could take from inside the app that
moved that site forward, and the pending sweeper is visibility-only by design, so
nothing moved it forward from outside either.

## What it does now

**A paid site is bought from the workspace credit balance**, in the publish
request, and deploys in that same request. One credit is one cent, so a $7/month
tier debits 700 credits.

- No payment gateway is involved and none has to be configured. That is what makes
  it work on a self-hosted deployment and on a workspace that has never bought a
  workspace plan.
- An underfunded wallet raises `credits.insufficient` (402) with **zero** side
  effects — nothing debited, the site left unpublished. The builder shows "Not
  enough workspace credits… top up in Settings → Billing".
- `Site.billing_rail` records which rail paid: `credits` for everything bought
  from now on, `addon` / `subscription` / `""` on rows sold before the cutover.

## What a plan CHANGE costs, which is not the sticker price

A first purchase pays the tier's whole monthly price. A **change** of tier does
not, because the period it lands in has already been bought:

| Move | Charged now | `renewal_date` |
|---|---|---|
| Buy a plan on an unplanned site | the tier's full month | now + 1 month |
| Move UP mid-period | the difference only | unchanged |
| Move DOWN mid-period | nothing | unchanged |
| Back UP to a tier already bought this period | nothing | unchanged |
| Down to the free floor | nothing | KEPT — the plan closes on that date |
| Monthly renewal | the current tier's full month | steps from the DUE date |

`Site.period_paid_usd` is what makes this work: the **most expensive tier already
bought for the current period**, in whole dollars. A change charges
`new_tier - period_paid_usd` and only when that is positive. It is a high-water
mark, so it does **not** fall on a downgrade — the customer paid for the dearer
tier and holds it until the period ends, which is why going back up inside that
period is free. It resets when a new period starts (the renewal sweep sets it to
the tier just charged) or when the site leaves its plan.

It is **not time-prorated**: an upgrade on day 29 costs the same difference as one
on day 2. The wallet has no proration machinery and a second pricing model in the
sites service is worse than a rule stated plainly. It rounds toward the customer
on a downgrade and away from them on a late upgrade, by at most the difference
between two rungs, and the Billing tab says so where the change is made.

Before this, every tier change was priced at the new tier's full month. That was
three overcharges at once: a downgrade cost more than the plan it was leaving,
flipping between two tiers charged a month per flip and could drain a funded
wallet, and an upgrade billed a period the customer had already paid for — then
reset `renewal_date`, handing back a free month that made the balance look
explicable.

### Cancelling runs to the end of the month already paid for

Dropping a paying site to the free floor does not end its plan on the spot. It
sets `Site.plan_cancels_at_period_end`, and everything the customer bought is
left standing — tier, `subscription_status: "active"`, `renewal_date`,
`period_paid_usd` — so the custom domain, analytics and badge removal survive to
the date they were paid up to. The renewal sweep closes it there: the row goes to
the free tier with no subscription, no renewal and nothing pre-paid, counted under
`closed` and logged at INFO, and nothing is debited.

Closing immediately, which is what the first cut of this did, is the mirror image
of the overcharges the rest of this document removes. The model is *the period is
bought, and a tier change re-prices it rather than restarting it* — that is why a
downgrade costs nothing and going back up inside the period is free. Ending the
plan the moment somebody clicks away contradicts it for the one move where it
costs the customer money: cancel on the 2nd and you forfeit 28 days **and** pay a
full month to come back, because the high-water mark is already zero.

Three consequences worth knowing before you touch this:

- **`_apply_site_plan` must not stamp the floor.** The request that schedules the
  close carries `site_plan_key="free"`, and entitlements resolve from `plan_tier`
  as much as from `subscription_status` — so writing the tier immediately would
  strip the capabilities mid-month while the flag sat there looking correct.
- **Resuming is a same-tier republish**, which is the one shape a tier change is
  written to ignore, so it is handled separately. It needs the explicit key and
  `purchase_authorized` (it re-arms a recurring charge). An unauthorized member
  republishing the same tier is not refused — that is their daily content edit —
  the close simply stays scheduled.
- **A site with no `renewal_date` closes immediately.** There is no period to
  honour, and the sweep selects on that date, so scheduling one would be a close
  nothing ever performs.

In the Billing tab: the date reads "Ends" rather than "Renews", picking the free
card explains what happens rather than saying "nothing more will be charged", and
the button on the current tier becomes **Resume plan** and is enabled — without
that last one, cancelling is one-way from the UI, because the picker correctly
shows the tier the site still holds.

### Who may make the change

Any member republishes a site freely — same tier, or no tier key at all — because
that is a content edit and it is what the people who build these sites do all day.
**Every move BETWEEN tiers needs `sites.buy_plan`, which is ADMIN**, and that
includes dropping to the free floor. Cancelling costs nothing, so a gate that only
watched for spending waved it through; it ends a plan the workspace is paying for
and takes the custom domain, the concierge and the badge removal off a live page,
with nothing refunded and nothing restored. Spending the company's money and
destroying what it already bought are the same decision seen from two sides.

A refused change writes nothing — the tier, the renewal date and the paid mark are
all left as the admin bought them. The caller gets `sites.plan_purchase_forbidden`
(403).

## Sites still on a pre-cutover rail cannot change plan

A site sold before the cutover is a line on the workspace's **Dodo** subscription,
and Dodo goes on invoicing it. `sync_site_addons` — the only code that could
adjust or cancel that line — was deleted with the rest of the gateway. So a tier
change on one of those rows is **refused**, with `sites.legacy_billing_rail`
(409), rather than served: charging the wallet would take payment on both rails
for the same site, and not charging would hand out a paid upgrade Dodo is still
billing at the old price.

The refusal is narrow. Republishing content on the plan the site already has is
untouched and still free, which is what those customers actually do day to day.
Only switching plans is blocked.

Find them:

```javascript
db.sites.find({
  subscription_status: "active",
  billing_rail: { $ne: "credits" },
  plan_tier: { $nin: ["free", null] },
})
```

Migrating one, **after** cancelling its line at the gateway:

```javascript
db.sites.updateOne(
  { _id: ObjectId("<site id>") },
  { $set: {
      billing_rail: "credits",
      period_paid_usd: <the monthly price of its current tier, in dollars>,
      renewal_date: ISODate("<when the period the customer already paid for ends>"),
  } },
)
```

Set `renewal_date` to the end of the period the gateway already charged for, not
to today: the sweep charges the wallet on that date, and bringing it forward bills
the customer twice for one month. `period_paid_usd` should be the price of the
tier they are on, so the first upgrade after the move charges a difference rather
than a whole month.

## What was removed

Dodo is gone from Paw Sites, not bypassed. Deleted rather than left unread,
because a field nothing consumes is one a later change quietly depends on again:

| Removed | Was |
|---|---|
| `POCKETPAW_DODO_SITE_PRODUCTS`, `POCKETPAW_DODO_SITE_ADDONS` | the two tier→gateway-id maps. Setting either env var is now inert |
| `SitePlanTier.dodo_product_id` / `.dodo_addon_id` and their resolvers | the catalog's gateway ids |
| `billing.service._site_addon_cart` / `sync_site_addons` | the workspace add-on cart, rebuilt from Site documents |
| `sites.service._publish_addon_site`, `_site_checkout_return_urls`, `mark_site_subscription` | the add-on purchase rail and the webhook's site writes |
| `SiteResponse.checkout_url` and the frontend redirect | the hosted-checkout hand-off |

**`dodo_plan_products` and the credit product id are untouched.** Workspace plans
and credit top-ups still bill through Dodo. Only the per-site ladder left.

`purchasable` therefore changed meaning rather than value: it asks "does the
ladder sell this tier one site at a time", true for every per-site rung and false
only for the org flats (`studio`, `agency`) that cover a whole workspace and are
sold by conversation.

## The one thing an operator must do

**Cancel any live per-site Dodo subscription in the Dodo dashboard.** Nothing in
the code will cancel one after this change — the lifecycle writes are gone.

The webhook still ACKS a delivery carrying a `site_id` and logs it at WARNING. A
line in the logs reading

```
billing.webhook: per-site subscription event type=... — Paw Sites no longer bills
through Dodo, so this is a subscription left live at the gateway.
```

means a customer is still being charged at the gateway for something the product
no longer sells. Find them:

```javascript
db.sites.find({ subscription_id: { $ne: null } }, { workspace: 1, plan_tier: 1, subscription_id: 1 })
```

`Site.subscription_id` is deliberately kept and no longer written: it is the only
record of which sites had a gateway subscription, and it is what that query needs.

**The routing fork on `site_id` stays for a reason.** Deleting it would not stop
those deliveries arriving; it would send them to the WORKSPACE path, which grants
credits and rewrites `Workspace.plan`. The two catalogs share the key `free`, so a
legacy per-site subscription on the floor would be read as a workspace plan
change. `tests/cloud/billing/test_dodo_webhook.py::test_a_site_carrying_delivery_never_grants_workspace_credits`
pins that.

## The renewal, which is the half that is easy to forget

A Dodo subscription is what used to make a monthly plan recur. A wallet-paid site
does not have one, so `sites/renewal_sweeper.py` does it: on the five-minute
heartbeat it debits the next month for every credits-paid site whose
`renewal_date` has passed and steps the date forward one month **from the due
date**, not from now.

When the wallet cannot cover a renewal the site is marked `cancelled` and drops to
the free floor — the attribution badge returns, the custom domain stops resolving,
the concierge goes quiet — and **the site itself stays live**. Republishing after a
top-up buys the tier again through the ordinary purchase path.

`POCKETPAW_SITE_RENEWALS_ENABLED=0` stops the sweep charging without a redeploy.

**A site on a legacy rail is not renewed from the wallet.** The sweep selects
`billing_rail == "credits"` only, so a site the gateway may still be billing is
never also debited. Once its gateway subscription is cancelled, flip it over by
setting `billing_rail` to `credits` and `renewal_date` to when its paid period
ends — after that the sweep picks it up like any other.

## A site that was charged and never went live

`activate_site` marks the subscription active **before** it runs the deploy, on
purpose: the badge stamper and the concierge embed both re-read the document
mid-deploy and resolve the site's paid capabilities from that field, so flipping
it afterwards branded a paying customer's page as free. The cost is that a deploy
which raises leaves the row `subscription_status: "active"` with
`deployed: false` — charged, and not up.

Two things follow, and both are deliberate:

- **The renewal sweep skips it.** It counts the row under `not_live` and logs at
  WARNING, leaving `renewal_date` in the past so the next tick re-examines it. It
  will not debit a second month for a page that 404s.
- **The buyer recovers it by republishing**, and that costs nothing. The site
  reads as already paying at the same tier, so no charge branch is entered; the
  republish just redeploys. Pinned by
  `test_a_failed_deploy_is_recovered_by_republishing_for_free`.

Nothing else surfaces these rows — the pending sweeper only looks for
`subscription_status: "pending"` — so the `not_live` count and its WARNING are
the operator signal. Find them:

```javascript
db.sites.find({ subscription_status: "active", deployed: false })
```

A dynamic site mid-provision has the same shape legitimately, for minutes. It
renews on the tick after it goes live.

## Recovering a site that is already stuck pending

Sites stranded by the old rail are still in the database. There is no migration:
the fix is the ordinary flow.

1. Make sure the workspace has credits (Settings → Billing → top up).
2. Open the site, pick the tier, and apply it.

The republish charges the wallet and deploys. Nothing has to be edited by hand,
and the stranded row is reused rather than duplicated — the site id is
deterministic per (workspace, pocket).

To find them:

```javascript
db.sites.find({ subscription_status: "pending", deployed: false })
```

## Before changing any of this

```bash
uv run python scripts/mutate.py --plan tests/mutations/site_plan_credits.json
```

Thirty mutations, each one a way to charge a customer wrongly: an
overdraftable purchase, a debit key that bills month two as month one, a renewal
that walks the billing day through the calendar, a lapse that takes the site down,
a republish billed as a purchase, a webhook falling through to the workspace
wallet, a tier change priced at a full month again, a legacy-rail site billed on
both rails, an upgrade that restarts the paid period, a keyless republish read
as a downgrade to free, a renewal billing a site that never deployed, and an
authorization gate narrowed back to purchases so a member can cancel a plan the
workspace pays for, a cancellation that closes on the spot and forfeits the month
already bought, a pending close that drops the paid tier immediately, a renewal
that charges a plan the customer already cancelled, and a resume that does not
call the close off. All are caught today; a plan with one rotted anchor applies none of them and reads as
covered while proving nothing, so run `--validate` repo-wide after editing the
code they point at.

`tests/mutations/site_plan_request_gate.json` is the sibling plan for the ask-an-
admin path, and it grew a mutation here too: a member who is refused can file a
Tray card instead, and since a cancellation is now refused the same way, that card
had to stop describing every request as a purchase.

That is not theoretical for this gate:
`tests/mutations/site_plan_purchase_authz.json` anchors three of its seven
mutations inside the same `if` block, and widening the gate to cover
cancellation silently rotted all three.

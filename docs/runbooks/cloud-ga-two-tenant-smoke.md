<!-- Runbook: GA acceptance — two-tenant isolation + live provision smoke for the
     Cloud Paw-OS multi-tenant offering. Created 2026-06-26 (GA-1).
     The AUTOMATED proof is tests/cloud/test_ga_two_tenant_pentest.py (9 tests,
     no creds, signed-webhook sim). THIS runbook is the captain's LIVE acceptance
     pass on the real Coolify box with real Dodo checkouts. Grounded against the
     shipped ISO-1..4 stack + the billing/credits/entitlements services. -->

# Cloud Paw-OS — Two-Tenant GA Smoke (live acceptance)

The final gate before the multi-tenant offering is sold: prove that **two paying
tenants on one shared backend are isolated end to end**, and that the
**signup → pay → provision → isolated** path works with a **real Dodo checkout**.

The automated half is already green and needs no credentials. This runbook is the
**live half** — the captain's manual acceptance on the deployed box.

---

## 0. What's already proven (automated, no creds)

Run the GA pen-test from the repo. It boots the cloud services in-process
(mongomock + the per-workspace SQLite stores), drives two tenants, and signs the
provision webhook with the real `standardwebhooks` library — so the verification
path is exercised exactly as production, with no live Dodo call.

```bash
uv run --group ee pytest tests/cloud/test_ga_two_tenant_pentest.py -v
# 9 passed — the GA gate:
#   Fabric / Instinct / Pockets / KB  → zero cross-leak between tenant A and B
#   fail-closed on every store path    → no-workspace under required-scope RAISES
#   entitlement                        → plan resolves to its feature set; isolated
#   credit 402                         → balance<=0 hard-blocks; isolated
#   provision (sim)                    → signed subscription.active stamps the plan
#                                        + grants monthly credits; tampered sig rejected
```

If that is not green, STOP — do not proceed to the live run.

The full isolation stack it exercises (ISO-1..4) must be merged first — see step 1.

---

## 1. Merge the isolation stack + redeploy `pocketpaw@dev`

The pen-test depends on the ISO-1..4 stack. Merge the stacked PRs in order
(base-first; the stack uses `--rebase` merges per the stacked-PR rule), then
redeploy.

PR stack (pocketpaw, all targeting `dev`):

| Order | PR | What | Merge |
|-------|----|------|-------|
| 1 | #1550 | ISO-1 — workspace-keyed store factory + Fabric isolation | `--rebase` (has stacked dependents) |
| 2 | #1551 | ISO-2 — Instinct isolation + per-workspace audit chains (+ test-double fix) | `--rebase` |
| 3 | #1554 | ISO-3 — ContextVar bridge + EE store provider | `--rebase` |
| 4 | #1555 | ISO-4 — migration + isolation lint guard + audit-lock fix | `--squash` (top of stack) |
| 5 | (this) | GA-1 — two-tenant pen-test + this runbook | `--squash` |

After merge, redeploy `pocketpaw@dev` to the Coolify box (the per-tenant
dedicated server / micro tier, per the deployment topology). The deploy must:

- **Enable fail-closed scoping**: set `POCKETPAW_REQUIRE_WORKSPACE_SCOPE=1` as a
  Coolify **runtime** var so a store call that ever loses its workspace context
  raises instead of reading a shared file. (The cloud bootstrap should also set
  this; the env var is the belt-and-suspenders.)
- **Run the one-time store migration** if this box already has shared-store data
  from before isolation (a box that only ever ran the isolated build can skip it):

  ```python
  # one-time, idempotent, reversible-safe (renames the shared files to .migrated)
  from pocketpaw.migrations.split_workspace_stores import migrate_shared_stores_to_workspaces
  import asyncio
  print(asyncio.run(migrate_shared_stores_to_workspaces()))
  ```

  It verifies the shared Instinct audit chain BEFORE re-chaining and ABORTS on
  tamper (pass `force=True` only after inspecting the breakage). Confirm the
  returned summary shows the expected per-workspace row counts and
  `source_chain_verified.intact == true`.

---

## 2. Wire Dodo creds + products (PAY-1) as Coolify RUNTIME vars

The live checkout needs real Dodo configuration. Set these as Coolify **runtime**
environment variables — NOT build vars (the 128 KB build-secret limit; and the
key must be present when the app serves requests, not just at build):

- `POCKETPAW_DODO_API_KEY` — the live Dodo API key.
- `POCKETPAW_DODO_WEBHOOK_SECRET` — the `whsec_…` signing secret for the Dodo
  webhook endpoint (the app verifies every inbound webhook against this).
- `POCKETPAW_DODO_PLAN_PRODUCTS` — the plan→product map, one real Dodo
  **recurring** product id per tier (e.g. `go=prod_…,pro=prod_…,pro_max=prod_…`).
  This is the PAY-1 deliverable; create one subscription product per tier in the
  Dodo dashboard first.
- `POCKETPAW_DODO_ENVIRONMENT` — `live_mode` (or `test_mode` for a dry run with
  Dodo test cards).

Point the Dodo webhook (in the Dodo dashboard) at the deployed
`POST /api/v1/billing/webhook` endpoint.

Verify config resolves before touching the UI:

```bash
# on the box / via the API — every tier returns a non-empty Dodo product id
curl -s "$BASE/api/v1/billing/plans" | jq '.[] | {key, dodo_product_id}'
```

If any tier's `dodo_product_id` is null, PAY-1 isn't finished — fix the product
map before continuing.

---

## 3. Live two-tenant walkthrough

Register **two real cloud users in two separate workspaces** (tenant A, tenant B)
and run the funnel for each. Use the self-serve onboarding (FE-1).

### 3a. Provision each tenant (signup → pay → provision)

For EACH tenant (A, then B), in a fresh browser session:

1. Register a new user → create the first workspace (`POST /api/v1/workspace`).
2. Pick a plan in the plan-picker (`GET /api/v1/billing/plans`).
3. `POST /api/v1/billing/subscribe` → follow the returned `checkout_url` to the
   **real Dodo hosted checkout** and complete payment (a real card, or a Dodo
   test card in `test_mode`).
4. Dodo fires `subscription.active` → the app's webhook stamps the workspace to
   the plan and grants the monthly credit allotment. Confirm on the box:
   - the workspace's plan flipped to the chosen tier (`GET /api/v1/workspace` or
     the billing panel, FE-2);
   - the credit balance shows the tier's monthly allotment
     (`GET /api/v1/credits/balance`).

Give A and B **different** plans (e.g. A=go, B=pro) so the entitlement check in
3c has a visible difference.

### 3b. Cross-leak probe (the core acceptance)

As **tenant A**, create distinctive data on each surface, then log in as
**tenant B** and confirm none of A's data is visible (and vice-versa):

- **Pockets** — A creates a pocket "A-SECRET"; B's pocket list must not show it.
- **Fabric** — A's agent/UI writes a Fabric object; B's Fabric query returns zero
  of A's objects. (Physically: A's rows live in
  `~/.pocketpaw/workspaces/<A>/fabric.db`, B's in `<B>/fabric.db` — verify the two
  files exist and are distinct on the box.)
- **Instinct** — A proposes an action; B's pending list + audit export must not
  contain it. `GET /api/v1/instinct/audit/verify` for each tenant verifies that
  tenant's OWN per-workspace chain (not a global one).
- **KB** — A ingests a document into a pocket scope; B's KB search must not
  surface it. A cross-workspace `search` for A's scope as B returns empty / 403.

Any single leak here is a **GA blocker**.

### 3c. Entitlement + credit enforcement

- **Plan gate** — exercise a feature gated to a higher tier from the lower-tier
  tenant; it must be denied (the per-plan ABAC feature set, resolved from
  `Workspace.plan`).
- **402 at zero** — drain one tenant's wallet (or use a fresh tenant with no
  allotment) and start a chat run; it must hard-block with a 402
  `credits.insufficient`. The OTHER tenant, with balance, is unaffected.

### 3d. Fail-closed spot check

Confirm `POCKETPAW_REQUIRE_WORKSPACE_SCOPE=1` is live: any code path that reaches
a store without a resolved workspace must error, never read a shared file. (This
is asserted automatically in the pen-test; on the box, a request that loses
tenancy returns an error rather than another tenant's data.)

---

## 4. Sign-off

GA is accepted when:

- [ ] the automated pen-test is green (`test_ga_two_tenant_pentest.py`, 9 passed);
- [ ] the ISO stack (#1550–#1555) is merged + `pocketpaw@dev` redeployed with
      `POCKETPAW_REQUIRE_WORKSPACE_SCOPE=1` and (if needed) the store migration run;
- [ ] Dodo creds + per-tier products are live runtime vars and
      `GET /billing/plans` returns a product id for every tier;
- [ ] both tenants provisioned via a **real Dodo checkout** (plan stamped +
      credits granted);
- [ ] **zero cross-leak** across Pockets, Fabric, Instinct, KB (3b);
- [ ] entitlement gate + 402-at-zero enforced and tenant-isolated (3c);
- [ ] fail-closed confirmed (3d).

When all boxes are checked, the offering is build-complete and smoke-passed for
sale to multiple paying tenants on one backend.

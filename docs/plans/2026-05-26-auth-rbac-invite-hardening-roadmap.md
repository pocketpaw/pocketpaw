# Auth / RBAC / Invitation Hardening — Roadmap

> Index for the three-wave hardening program. Each wave is its own executable
> plan; this doc maps audit findings → wave → task so nothing is lost.

## Background

May 2026 audit of `backend/ee/pocketpaw_ee/cloud/` and the matching
paw-enterprise UI found 25 issues in auth, RBAC, and the invitation flow,
plus 20 UX gaps in the surrounding surfaces. Findings ordered by severity:

- **Critical / security holes** — 5 items (active-workspace IDOR, plaintext
  invite tokens, no email match on accept, no rate limits, no single-use
  invites).
- **RBAC / authorization gaps** — 5 items (route guard inconsistency,
  group-agent IDOR, plan lookup fail-open, last-owner protection,
  realtime channel auth).
- **Invitation flow** — 5 items (stale GC, audit trail, token leakage,
  pre-assigned role trust, soft-deleted accept).
- **Missing enterprise features** — 9 items (MFA, SSO, SCIM, API keys,
  sessions, password policy, audit API, JIT deprovisioning, CSRF).
- **UX** — 20 items grouped into invite flow (U1–U7),
  members management (U8–U11, U16–U20), and auth surfaces (U12–U15).

## Three waves

| Wave | Plan | Goal | Ships |
|------|------|------|-------|
| **1** | [`…-wave1.md`](./2026-05-26-auth-rbac-invite-hardening-wave1.md) | Close the 5 P0 security holes + matching invite UX | ~1 sprint, 2 PRs (backend + UI) |
| **2** | [`…-wave2.md`](./2026-05-26-auth-rbac-invite-hardening-wave2.md) | Members page + medium-severity backend + audit log | ~2 sprints, 2 PRs |
| **3** | [`…-wave3.md`](./2026-05-26-auth-rbac-invite-hardening-wave3.md) | Enterprise table stakes (MFA, SSO, API keys, sessions) | ~quarter, ~9 PRs |

## Finding → Wave → Task map

### Critical / security holes

| # | Finding | Wave | Task |
|---|---|---|---|
| 1 | `set_active_workspace` no membership check | 1 | Task 2 |
| 2 | Invite tokens stored plaintext | 1 | Task 3 |
| 3 | `accept_invite` doesn't verify email | 1 | Task 5 |
| 5 | No rate limiting on login/register/invite | 2 | Task 1 |
| 6 | Invite token no single-use lock | 1 | Task 4 |

### RBAC / authorization gaps

| # | Finding | Wave | Task |
|---|---|---|---|
| 7 | Inconsistent route-level guards | 2 | Task 6 (per-route audit) |
| 8 | Group-agent route doesn't verify agent.workspace | 2 | Task 9 |
| 9 | `get_workspace_plan` fails open | 2 | Task 8 |
| 10 | Last-owner / self-demote not protected | 2 | Task 4 |
| 11 | Realtime channel auth gap | 2 | Task 11 |

### Invitation flow

| # | Finding | Wave | Task |
|---|---|---|---|
| 12 | Stale invites not GC'd | 2 | Task 2 |
| 13 | No audit trail for invite actions | 2 | Task 10 |
| 14 | Pre-assigned role trusted at accept | 1 | Task 5 (email match closes this) |
| 15 | Re-invite privilege not re-checked | 2 | Task 6 |
| 16 | Soft-deleted workspace acceptable | 2 | Task 5 |

### Missing enterprise features

| # | Finding | Wave | Task |
|---|---|---|---|
| 17 | No MFA / 2FA | 3 | Tasks 3, 4, 5 |
| 18 | No SSO (SAML / OIDC) | 3 | Tasks 10, 11 (OIDC); SAML deferred |
| 19 | No domain capture | 3 | Task 12 |
| 20 | No API keys / service accounts | 3 | Tasks 8, 9 |
| 21 | No session listing / revoke | 3 | Tasks 6, 7 |
| 22 | No password policy / HIBP | 3 | Task 1 |
| 23 | No audit log API | 2 (rows) + 3 (export/webhooks) | W2 T10 + W3 T15 |
| 24 | No JIT deprovisioning | 3 | Task 14 |
| 25 | CSRF not verified on cookie POSTs | 3 | Task 13 |

### UX

| # | UX gap | Wave | Task |
|---|---|---|---|
| U1 | New-user path forced under invited email only | 1 | Task 8 (state machine has ready_wrong_user) |
| U2 | No "sign out, use different account" | 1 | Task 8 |
| U3 | Failures collapse into one error string | 1 | Task 6 (preview) + Task 8 |
| U4 | FE/BE password rules disagree | 1 (12-char floor) + 3 (full policy) | W1 T8 + W3 T1 |
| U5 | No org logo / inviter context on accept page | 2 | Task 13 (members API hands the inviter back; expand preview later) |
| U6 | Member onboarding fires for already-onboarded users | 1 | Task 8 |
| U7 | No Decline button | 1 (UI placeholder) + 2 (BE endpoint) | W1 T8 + W2 T3 |
| U8 | No members page | 2 | Task 13–14 |
| U9 | No role explainer | 2 | Task 14 |
| U10 | No blast-radius preview on delete | 2 | Task 19 |
| U11 | No last-owner UI guard | 2 | Task 14 |
| U12 | No active-sessions UI | 3 | Task 7 |
| U13 | No password reset / verify UI | 3 | Task 2 |
| U14 | No MFA enrollment UI | 3 | Task 5 |
| U15 | No workspace switcher | 3 | Task 16 |
| U16 | No bulk invite | 2 | Tasks 7 (BE) + 18 (UI) |
| U17 | No resend cooldown | 2 | Task 15 |
| U18 | No owner-role hard-confirm | 2 | Task 17 |
| U19 | No copy-link button | 2 | Tasks 15 + 16 |
| U20 | No who-invited-whom history view | 2 | Task 20 |

## Sequencing rules

- **Wave 1 lands first.** It removes the bleeding. Don't start Wave 2 until
  Wave 1 is in `main` and a senior reviewer has signed off on the
  hash-and-email-match path.
- **Wave 2 is parallelizable.** Backend (Tasks 1–12) and frontend
  (Tasks 13–20) can run in two streams; the frontend mocks the API for
  pages whose backend hasn't landed.
- **Wave 3 ships per feature.** Each feature is its own PR; user lands
  them in priority order based on customer demand. Suggested default:
  password policy → MFA → sessions → API keys → SSO → domains → CSRF →
  JIT → audit export.

## What's NOT in this program

- **General authn modernization** (passkeys, WebAuthn, hardware keys) —
  out of scope; treat as a Wave 3.5 if a customer asks.
- **SAML 2.0** — deferred (see Wave 3 footer).
- **SCIM** — deferred; depends on SSO being live and a customer asking.
- **Pen-test third-party engagement** — schedule for after Wave 1 lands.

## Resume points

If work pauses mid-program, the conversation entry point is:
- `D:/paw/backend/docs/plans/2026-05-26-auth-rbac-invite-hardening-wave<N>.md`
- Memory: `project_auth_invite_hardening.md` tracks which wave is in
  progress.

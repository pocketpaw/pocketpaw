# Auth / RBAC / Invitation Hardening — Wave 3

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship the enterprise-table-stakes auth features procurement teams ask about: MFA, SSO, API keys, session management, password policy. Without these the product is "consumer SaaS in a suit"; with them it's an enterprise SKU.

**Architecture:**
- **Backend:** layer on top of fastapi-users — TOTP MFA via `pyotp` and per-user backup codes; API keys as a parallel auth backend; session tracking via JWT jti + Redis revocation set; SSO via `authlib` OIDC (SAML deferred to a follow-up given complexity); password policy + HIBP k-anon check; CSRF on cookie POSTs; admin audit log API and JIT deprovisioning hook.
- **Frontend:** four new settings panes — Security (password / MFA / sessions), API keys, SSO (admin only), Domain capture (admin only). A workspace switcher in the avatar menu.

**Tech Stack:**
- `pyotp`, `qrcode[pil]` for MFA
- `authlib` for OIDC; defer SAML to a Wave 3.5
- `passlib[argon2]` (already used) + custom validators
- `httpx` + HIBP k-anon (`api.pwnedpasswords.com/range/{first5}`)
- `slowapi` + Redis (already in from Wave 2)
- Svelte 5 + shadcn-svelte AlertDialog / OTP Input / Dropzone

**Depends on:** Wave 2 in main. Specifically the audit collection (Task 10 in Wave 2), the rate limiter, and the members page (some Wave 3 UI plugs into it).

**Branches:** one branch per logical feature — MFA, API keys, sessions, SSO, password policy. Each is its own PR; user can land them in any order after the first lands.

---

## Pre-flight

### Task 0: Confirm Wave 2 in main + Redis configured

```bash
cd D:/paw/backend
git log --oneline origin/main -50 | grep -i "audit\|rate.limit\|bulk.invite"
```

Both should appear. Then verify `POCKETPAW_REDIS_URL` is set in the dev env (required for sessions + rate limits).

---

## Feature 1 — Password policy + HIBP (Finding #22)

### Task 1: Custom password validator on register and change

**Files:**
- Modify: `D:/paw/backend/ee/pocketpaw_ee/cloud/auth/core.py` (UserManager)
- Create: `D:/paw/backend/ee/pocketpaw_ee/cloud/auth/password_policy.py`
- Test: `tests/cloud/auth/test_password_policy.py`

**Step 1: Policy rules.**

- Min length 12
- At least one of each: upper, lower, digit, symbol
- Not equal to email local-part
- HIBP breach check: ≥1 breach hit = rejected (with a clear UX: "this password appears in known data breaches")

**Step 2: HIBP client.** k-anonymity API: SHA-1 the password, send the first 5 hex chars, read back the list of suffixes, check if our suffix is in it. Cache by hash for 24h to keep things snappy and dodge rate limits. Provide `POCKETPAW_HIBP_ENABLED=true|false` (default true in prod, false in tests).

**Step 3: Hook into fastapi-users.** Override `validate_password` on `UserManager`. Raise `InvalidPasswordException` with a typed reason ("too_short" / "missing_class" / "breached") so the frontend can format the message.

**Step 4: Tests.** Each rule has a failing-then-passing case. HIBP test stubs `httpx.AsyncClient` to return a deterministic suffix list.

**Step 5: Commit.** `feat(auth): enforce password policy + HIBP breach check`.

### Task 2: Forgot password / verify email — wire if missing

**Files:**
- Audit `auth/router.py` for `/auth/forgot-password` / `/auth/reset-password` / `/auth/request-verify-token` / `/auth/verify` — fastapi-users provides them but they might not be `include_router`'d.
- Modify if absent: include the missing sub-routers.
- Create: `paw-enterprise/src/routes/auth/forgot/+page.svelte`, `.../reset/+page.svelte`, `.../verify/+page.svelte` (UX U13)

**Step 1:** Audit + include.

**Step 2:** UI for each: email entry → toast "Check your email"; token-bearing page that posts to backend; success / failure states.

**Step 3: Commit (one each).** `feat(auth): wire forgot/reset/verify routes` + `ui(auth): password reset and email verification pages`.

---

## Feature 2 — MFA / TOTP (Finding #17)

### Task 3: TOTP secret + enrollment

**Files:**
- Modify: `backend/ee/pocketpaw_ee/cloud/models/user.py` — add `mfa_totp_secret: str | None`, `mfa_enabled: bool`, `mfa_backup_codes: list[str]` (hashed), `mfa_verified_at`.
- Create: `backend/ee/pocketpaw_ee/cloud/auth/mfa.py` (service module)
- Modify: `auth/router.py` — `POST /auth/mfa/setup`, `POST /auth/mfa/verify`, `POST /auth/mfa/disable`, `POST /auth/mfa/backup-codes/regenerate`
- Test: `tests/cloud/auth/test_mfa.py`

**Step 1: Setup endpoint.** Returns `{secret, otpauth_url, qr_svg}`. Don't flip `mfa_enabled=true` yet — that happens on verify.

**Step 2: Verify endpoint.** Takes a 6-digit code; if valid, flip `mfa_enabled=true`, generate 10 backup codes (hash + return plaintext once), audit-log it.

**Step 3: Disable endpoint.** Requires current password + valid TOTP. Audit-log.

**Step 4: Backup code regenerate.** Requires password + TOTP, invalidates all old codes.

**Step 5: Tests.** pyotp generates a known TOTP from a known secret + timestamp → use that for deterministic verify.

**Step 6: Commit.** `feat(auth): TOTP MFA enrollment, verify, disable, backup codes`.

### Task 4: Enforce MFA at login

**Files:**
- Modify: `auth/core.py` UserManager `on_after_login` and the login flow

**Step 1: Two-step login.** If `user.mfa_enabled`, the first `/auth/login` returns a short-lived (5 min) `mfa_pending` token *instead of* the auth cookie. The client posts the TOTP code + the pending token to `POST /auth/mfa/challenge`. On success, mint the real auth cookie.

**Step 2: Backup code path.** Same `/auth/mfa/challenge` route accepts a backup code; consume it (hash-match + mark used).

**Step 3: Tests.** Login with MFA on → first response has no cookie; challenge with valid code → cookie issued; challenge with wrong code 5x → 429.

**Step 4: Commit.** `feat(auth): enforce MFA challenge at login`.

### Task 5: MFA enrollment UI (UX U14)

**Files:**
- Create: `paw-enterprise/src/routes/settings/security/+page.svelte`
- Create: shadcn `OtpInput` if not present (use the existing component or shim)

**Step 1: Page sections.** Password change (top), MFA (middle), Active sessions (bottom — placeholder for Task 7).

**Step 2: MFA flow.** "Enable two-factor authentication" → wizard:
- Show QR + manual secret with a "Copy" button
- OTP input — type a code from your authenticator
- On success: show 10 backup codes with a "Download as TXT" + "I've saved these" gate
- Confirm box: "If you lose your device AND your backup codes, support cannot recover your account"

**Step 3: Disable flow.** Confirm with password + current TOTP.

**Step 4: Login flow.** Update the login page to detect the `mfa_pending` response and show the OTP step with a "use backup code" link.

**Step 5: Commit (two).** `feat(security): MFA enrollment + management UI` and `feat(auth): MFA challenge on login`.

---

## Feature 3 — Session management (Finding #21)

### Task 6: Track session jtis + revoke endpoint

**Files:**
- Modify: `auth/core.py` JWT strategy — embed `jti` (fastapi-users does, just need to read it)
- Create: `backend/ee/pocketpaw_ee/cloud/auth/sessions.py`
- Modify: `auth/router.py` — `GET /auth/sessions`, `DELETE /auth/sessions/{jti}`, `POST /auth/sessions/revoke-others`

**Step 1: Session row.** On login, write to a `sessions` collection: `{user_id, jti, ip, user_agent, device_fingerprint, issued_at, last_seen_at, revoked: false}`.

**Step 2: Revocation set.** Redis set `revoked_jti:{user_id}` containing revoked jti's; each authenticated request checks membership (cached for the request lifetime to keep latency flat).

**Step 3: Routes.** List filters out revoked. Revoke one adds to the set + updates the row. Revoke-others revokes everything except the caller's jti.

**Step 4: Tests.** Login twice from two contexts (different UAs) → two rows; revoke first → first session's requests now 401; second still works.

**Step 5: Commit.** `feat(auth): per-session tracking + revoke endpoints`.

### Task 7: Sessions UI (UX U12)

**Files:**
- Modify: `paw-enterprise/src/routes/settings/security/+page.svelte` (add the Sessions section)

**Step 1: List.** Per row: device label ("Chrome · macOS · Mumbai · 2h ago"), with a small "This device" badge for the current jti. Row action: Revoke.

**Step 2: Revoke all others.** Top action button. Confirm dialog.

**Step 3: Auto-refresh** the list after revoke (optimistic, then re-fetch).

**Step 4: Commit.** `ui(security): active sessions list with per-row revoke`.

---

## Feature 4 — API keys / service accounts (Finding #20)

### Task 8: API key model + auth backend

**Files:**
- Create: `backend/ee/pocketpaw_ee/cloud/models/api_key.py`
- Create: `backend/ee/pocketpaw_ee/cloud/auth/api_keys.py`
- Modify: `auth/router.py` — `POST/GET/DELETE /workspaces/{id}/api-keys` + key-by-key revoke
- Modify: request-context dep — accept `Authorization: Bearer paw_<key>` as an alternative to JWT, resolve to `(user_id, workspace_id, scopes)`

**Step 1: Model.**

```python
class APIKey(Document):
    workspace: Indexed(str)
    owner_user_id: str
    name: str                 # human label
    prefix: Indexed(str)      # first 8 chars for identification (not secret)
    hashed_secret: str        # bcrypt of the full key
    scopes: list[str]         # ["chat.send", "files.read", ...]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked: bool = False
```

Key format: `paw_<prefix><secret>` — total ~32 chars. Stored: prefix (plain) + bcrypt(full).

**Step 2: Auth resolution.** On Bearer token: if it starts with `paw_`, route to API-key path (look up by prefix, bcrypt-compare). Else hand to JWT.

**Step 3: Scopes.** Define a registry (constant) of available scopes. Default new key gets a workspace-scoped read-only set.

**Step 4: Audit-log every key use** at the dep level (sample to avoid log spam — once per 60s per key).

**Step 5: Tests.** Issue key → use to call a chat-read endpoint → success; use to call a chat-write endpoint without `chat.send` scope → 403; revoke → 401.

**Step 6: Commit.** `feat(auth): API keys with scopes`.

### Task 9: API keys UI

**Files:**
- Modify: `paw-enterprise/src/routes/settings/api-keys/+page.svelte` (route already exists per the audit)

**Step 1: List.** Name, prefix (`paw_abcd…`), scopes (chip list), last-used, expires. Actions: Revoke.

**Step 2: Create wizard.** Name → scopes (multiselect with descriptions) → expiry (Never / 30d / 90d / 1y) → Generate. Show the full key ONCE with a Copy button and a "Save this — you won't see it again" warning. Cannot dismiss without confirming copied.

**Step 3: Commit.** `ui(api-keys): create + manage workspace API keys`.

---

## Feature 5 — SSO via OIDC (Finding #18)

### Task 10: OIDC provider config + login flow

**Files:**
- Create: `backend/ee/pocketpaw_ee/cloud/auth/sso/{__init__,oidc.py,domain.py,router.py,service.py}`
- Modify: `models/workspace.py` — add `sso_config: SsoConfig | None` (or a separate `sso_configs` collection keyed by workspace)
- Test: `tests/cloud/auth/test_sso.py`

**Step 1: Config DTO.**

```python
class SsoConfig(BaseModel):
    provider: Literal["okta", "google", "azure", "generic_oidc"]
    issuer: str               # https://acme.okta.com
    client_id: str
    client_secret: str        # encrypted at rest (Fernet w/ a key derived from the deploy secret)
    allowed_domains: list[str]  # @acme.com — auto-join
    enforced: bool            # if true, password login is disabled for matching emails
```

**Step 2: Endpoints.**

- `GET /auth/sso/{workspace_slug}/login` → redirect to provider
- `GET /auth/sso/callback?code=&state=` → exchange, look up / create user, mint session
- `POST /workspaces/{id}/sso` (admin) → create/update config
- `DELETE /workspaces/{id}/sso` (admin) → remove

**Step 3: authlib integration.** Use `authlib.integrations.starlette_client.OAuth` to handle the dance. Store transient `state` in Redis.

**Step 4: Just-in-time provisioning.** If the email domain matches `allowed_domains`, auto-create user + auto-join workspace as `member`. Audit-log it.

**Step 5: Tests.** Use `responses` or `httpx` MockTransport to stub the OIDC provider. Happy path + state mismatch + email-domain mismatch + JIT user creation.

**Step 6: Commit.** `feat(auth): OIDC SSO with JIT provisioning`.

### Task 11: SSO admin UI

**Files:**
- Create: `paw-enterprise/src/routes/settings/workspace/sso/+page.svelte`

**Step 1: Provider chooser.** Okta / Google / Azure AD / Generic OIDC (each ships with a preset issuer + the standard scopes).

**Step 2: Config form.** Client ID, client secret (with reveal-toggle), allowed domains (chip input), enforce-for-domain toggle.

**Step 3: Test connection button.** Backend `POST /workspaces/{id}/sso/test` — does discovery + token exchange dry-run. Surface errors clearly.

**Step 4: Login page update.** If user types an email matching an enforced SSO domain, swap the password field for a "Continue with SSO" button.

**Step 5: Commit (two).** `feat(sso): admin config UI` + `ui(auth): SSO login redirect for enforced domains`.

---

## Feature 6 — Domain capture (Finding #19)

### Task 12: Verified domains for auto-join

**Files:**
- Modify: `models/workspace.py` — add `verified_domains: list[VerifiedDomain]`
- Modify: `workspace/service.py` — verification flow
- Modify: register flow (UserManager) — auto-add to workspace if email domain matches

**Step 1: Verification.** Admin enters domain → backend generates `paw-verify=<token>` TXT record string. Admin adds it to DNS, clicks Verify; backend `dns.resolver` looks up TXT records, matches, flips `verified=true`.

**Step 2: Auto-join.** In `on_after_register`, look up workspaces with `verified_domains.value == user.email.split('@')[1] AND verified=true`. If exactly one match with `auto_join=true`, add the user as `member`.

**Step 3: Tests.** DNS resolver stubbed; verify happy path + bad TXT.

**Step 4: UI.** Add a Domains section to `/settings/workspace`. List of domains with status (Pending / Verified). Add → show the TXT record with copy + Verify button.

**Step 5: Commit.** `feat(workspace): verified domains with auto-join`.

---

## Feature 7 — CSRF on cookie POSTs (Finding #25)

### Task 13: CSRF middleware for cookie auth

**Files:**
- Create: `backend/ee/pocketpaw_ee/cloud/_core/csrf.py`
- Modify: middleware mount in the FastAPI app setup
- Modify: `paw-enterprise/src/lib/core/shared/http.ts` — inject the CSRF header

**Step 1: Token issuance.** On login (cookie path), set a non-HttpOnly `paw_csrf` cookie with a random token. JS can read it.

**Step 2: Middleware.** For any `POST/PUT/PATCH/DELETE` to `/api/v1/*` carrying the auth cookie, require header `X-Paw-Csrf: <value>` matching the cookie. Bearer-token (Tauri) requests skip this — they're not subject to browser CSRF.

**Step 3: Tests.** POST with cookie + missing header → 403; with mismatched header → 403; with bearer only → pass.

**Step 4: Client.** `http()` wrapper reads the cookie and sets the header automatically.

**Step 5: Commit.** `feat(security): CSRF protection for cookie-authenticated POSTs`.

---

## Feature 8 — JIT deprovisioning (Finding #24)

### Task 14: Revoke everything on member removal

**Files:**
- Modify: `workspace/service.py` `remove_member`

**Step 1: On remove, in order:**

1. Revoke all of the user's API keys scoped to this workspace.
2. Revoke all of the user's sessions (force re-login; if they're in other workspaces they get pushed into a re-pick).
3. Mark pending invites the user issued for this workspace as `revoked_reason="inviter_removed"`.
4. Kick their realtime subscriptions (use the audience resolver invalidation).
5. Audit-log the removal with cascade detail.

**Step 2: Tests.** Cover each cascade.

**Step 3: Commit.** `feat(workspace): cascade revoke on member removal`.

---

## Feature 9 — Admin audit log API (Finding #23)

### Task 15: Promote Wave 2's audit table to a workspace-scoped UI

This is already partially done in Wave 2 (Task 10 + Task 20). Wave 3 adds:

**Step 1: Export.** `GET /workspaces/{id}/audit/export?since=&until=` → CSV (newline-delimited, no large memory blow up — stream).

**Step 2: Webhook.** `POST /workspaces/{id}/audit/webhooks` to register an external HTTPS endpoint that receives audit events as POSTs (SIEM integration). Sign with HMAC.

**Step 3: Tests + UI.** Add Export button to the audit page (Wave 2 wired it). Webhooks UI as a small section under Settings → Security.

**Step 4: Commit.** `feat(audit): CSV export + SIEM webhook delivery`.

---

## Feature 10 — Workspace switcher (UX U15)

### Task 16: Avatar-menu workspace switcher

**Files:**
- Modify: paw-enterprise avatar menu component (find it: `grep -rn "activeWorkspace" src/lib/components/`)

**Step 1: Listing.** Show all `authStore.user.workspaces`. Current one has a check mark. Each row shows name + role chip.

**Step 2: Switch.** Click → call `setActiveWorkspace(workspaceId)`. Wave 1's backend now refuses non-member ids — surface 403 gracefully (toast: "You're no longer a member of that workspace") and remove from the list.

**Step 3: Create new.** "+ New workspace" CTA at the bottom routes to a create form.

**Step 4: Commit.** `ui(workspaces): avatar-menu switcher with role chips`.

---

## Bookkeeping

### Task 17: Memory updates

Flip Wave 1 / 2 / 3 statuses. Add reference memories where appropriate (e.g., `reference_sso_provider_presets.md` if the OIDC presets become a repeated lookup).

---

## What we deferred from Wave 3

- **SAML 2.0** — complexity vs return. Most modern IdPs support OIDC; SAML is a procurement check-the-box. Park as Wave 3.5 if a customer specifically asks.
- **SCIM provisioning** — depends on SSO being live and a customer asking. Wave 3.5.
- **WebAuthn / passkeys** — TOTP is the table stake; passkeys are nice-to-have. Wave 3.5.
- **Hardware security keys (FIDO2)** — same bucket.

---

## Roll-up

- [ ] Task 0  baseline
- [ ] Task 1  password policy + HIBP
- [ ] Task 2  forgot/reset/verify pages
- [ ] Task 3  TOTP enrollment backend
- [ ] Task 4  MFA at login
- [ ] Task 5  MFA UI
- [ ] Task 6  sessions backend
- [ ] Task 7  sessions UI
- [ ] Task 8  API keys backend
- [ ] Task 9  API keys UI
- [ ] Task 10 OIDC backend
- [ ] Task 11 SSO admin UI + login redirect
- [ ] Task 12 verified domains
- [ ] Task 13 CSRF
- [ ] Task 14 JIT deprovisioning
- [ ] Task 15 audit export + webhooks
- [ ] Task 16 workspace switcher
- [ ] Task 17 memory bookkeeping

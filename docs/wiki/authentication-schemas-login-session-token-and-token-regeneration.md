---
{
  "title": "Authentication Schemas — Login, Session Token, and Token Regeneration",
  "summary": "The auth schemas define the request and response shapes for PocketPaw's cookie-based authentication flow, including master token login, session token exchange, and token regeneration. These three models represent the full lifecycle of an authenticated session.",
  "concepts": [
    "authentication",
    "master token",
    "session token",
    "LoginRequest",
    "SessionTokenResponse",
    "TokenRegenerateResponse",
    "token lifecycle",
    "single-user auth",
    "token regeneration"
  ],
  "categories": [
    "authentication",
    "schemas",
    "security"
  ],
  "source_docs": [
    "a3e81ec8bcf109f7"
  ],
  "backlinks": null,
  "word_count": 423,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw uses a two-layer authentication model: a master access token (set during initial setup) and short-lived session tokens issued for individual browser sessions. The auth schemas capture the payloads at each step of this flow.

## `LoginRequest`

```python
class LoginRequest(BaseModel):
    token: str = Field(..., description="Master access token")
```

The login endpoint accepts a single field: the master token. There is no username/password combination because PocketPaw is a single-user application — the owner is whoever holds the master token. Keeping the login payload to one field also reduces the attack surface: there is no username enumeration vector, no separate password reset flow, and no credential stuffing risk from leaked username lists.

## `SessionTokenResponse`

```python
class SessionTokenResponse(BaseModel):
    session_token: str
    expires_in_hours: int
```

On successful login, the server issues a time-limited session token. Returning `expires_in_hours` as an integer (rather than an absolute expiry timestamp) lets the client calculate the expiry relative to its own clock, avoiding issues with clock skew between server and client. The browser dashboard uses this to show when the session will expire and to trigger re-authentication proactively.

Session tokens have a shorter lifetime than the master token, which means a compromised session token has a bounded window of validity. The master token itself never travels over the wire after initial login — only session tokens are used for ongoing API calls.

## `TokenRegenerateResponse`

```python
class TokenRegenerateResponse(BaseModel):
    token: str
```

This schema is returned when the master token is regenerated via an admin action. The new token is shown once in the response; the old token is invalidated immediately. This is the recovery path if the master token is compromised: regenerate, receive once, update any API clients.

The field is named `token` (not `master_token` or `access_token`) which is slightly ambiguous, but consistent with the `LoginRequest.token` naming convention — both refer to the master token.

## Session vs. Master Token

| Property | Master Token | Session Token |
|---|---|---|
| Lifetime | Until regenerated | Hours (configurable) |
| Used in | LoginRequest, TokenRegenerateResponse | All API calls after login |
| Storage | User-controlled secret | HTTP-only cookie |
| Exposure | Never in API responses after setup | Every authenticated request |

## Known Gaps

- `expires_in_hours` is an integer, which cannot express sub-hour expiry windows. A production system that wanted 30-minute sessions would need to change this to `expires_in_seconds` or use an absolute timestamp.
- There is no `RefreshTokenRequest` schema. Session tokens that expire require a full re-login with the master token; there is no silent renewal mechanism.

---
{
  "title": "OAuth Manager — Google OAuth 2.0 Authorization Code Flow and Token Lifecycle",
  "summary": "`OAuthManager` implements the full OAuth 2.0 authorization code flow for Google (and optionally Spotify), handling authorization URL generation, code-for-token exchange, access token refresh, and token revocation, backed by `TokenStore` for persistent credential storage. It provides a `get_valid_token()` convenience method that transparently refreshes expired tokens.",
  "concepts": [
    "OAuth 2.0",
    "OAuthManager",
    "authorization code flow",
    "token refresh",
    "get_valid_token",
    "TokenStore",
    "CSRF state",
    "access_type offline",
    "prompt consent",
    "PKCE",
    "Google OAuth",
    "Spotify OAuth",
    "revoke token"
  ],
  "categories": [
    "integrations",
    "authentication"
  ],
  "source_docs": [
    "ff3d90d71b08e9e3"
  ],
  "backlinks": null,
  "word_count": 515,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`oauth.py` is the authentication backbone for all of PocketPaw's Google integrations. Rather than implementing OAuth logic inside each service client (Gmail, Calendar, Drive, Docs), `OAuthManager` centralises the entire token lifecycle in one class backed by a persistent `TokenStore`.

## Provider Registry

```python
PROVIDERS: dict[str, dict[str, str]] = {
    "google": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "revoke_url": "https://oauth2.googleapis.com/revoke",
    },
    "spotify": {
        "auth_url": "https://accounts.spotify.com/authorize",
        "token_url": "https://accounts.spotify.com/api/token",
        "revoke_url": "",
    },
}
```

The `PROVIDERS` dict makes `OAuthManager` extensible to new OAuth providers by adding an entry — no subclassing required. Spotify's entry shows that not all providers have a revoke endpoint (`revoke_url: ""`), which the `revoke_token()` method must handle gracefully.

## Authorization URL Generation

```python
def get_auth_url(self, provider, client_id, redirect_uri, scopes, state="") -> str:
    params = {
        "client_id": client_id, "redirect_uri": redirect_uri,
        "response_type": "code", "scope": " ".join(scopes),
        "access_type": "offline", "prompt": "consent",
    }
    return f"{config['auth_url']}?{urllib.parse.urlencode(params)}"
```

`access_type=offline` requests a refresh token alongside the access token — without it, Google only issues short-lived access tokens with no way to refresh them. `prompt=consent` forces the consent screen even if the user has previously authorized, which is necessary to obtain a new refresh token (Google only issues refresh tokens on the first authorization or when consent is explicitly re-requested).

The `state` parameter is used for CSRF protection: the caller generates a random state value, stores it in the session, and verifies it matches the state returned in the OAuth callback.

## Code Exchange

```python
async def exchange_code(self, provider, service, code, client_id, client_secret, redirect_uri, scopes=None) -> OAuthTokens:
```

`exchange_code()` POSTs the authorization code to the provider's token endpoint and stores the resulting `OAuthTokens` (access token, refresh token, expiry) in `TokenStore` under the `service` key. The `service` key (e.g., `"google_gmail"`) scopes tokens per-service, allowing the same Google OAuth client credentials to manage independent tokens for Gmail, Calendar, and Drive simultaneously.

## Token Refresh

```python
async def refresh_token(self, provider, service, client_id, client_secret) -> OAuthTokens | None:
    tokens = self.store.load(service)
    resp = await client.post(config["token_url"], data={
        "grant_type": "refresh_token",
        "refresh_token": tokens.refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    })
```

If refresh fails (e.g., the refresh token was revoked), the method returns `None`. The service clients handle `None` by raising `RuntimeError` with a re-authentication message.

## get_valid_token — Transparent Refresh

```python
async def get_valid_token(self, provider, service, client_id, client_secret) -> str | None:
    tokens = self.store.load(service)
    if tokens.expires_at and tokens.expires_at > time.time() + 60:
        return tokens.access_token
    refreshed = await self.refresh_token(provider, service, client_id, client_secret)
    return refreshed.access_token if refreshed else None
```

The 60-second buffer before expiry prevents using a token that expires mid-request. This is the method called by all Google service clients — they never touch the token store directly.

## Known Gaps

- The `provider` parameter is passed to `exchange_code()` and `refresh_token()` but the service clients always infer it as `"google"` without exposing it to callers. A Spotify integration would need to pass `"spotify"` explicitly.
- `revoke_token()` silently succeeds when `revoke_url` is empty (Spotify). A caller that expects revocation to fail loudly when unsupported would need to check this.
- There is no PKCE (Proof Key for Code Exchange) support, which is recommended for public clients in modern OAuth 2.1 guidance.

---
{
  "title": "OAuth2 Server — Authorize, Token Exchange, and Revoke",
  "summary": "The OAuth2 router implements the server side of the OAuth2 authorization code flow, allowing third-party MCP clients and tools to obtain scoped access tokens to PocketPaw's API. It handles the consent UI, code-to-token exchange, refresh token rotation, and revocation.",
  "concepts": [
    "OAuth2",
    "authorization code flow",
    "consent form",
    "token exchange",
    "refresh token",
    "token revocation",
    "MCP client",
    "PKCE",
    "scope delegation",
    "OAuthServer",
    "RFC 6749"
  ],
  "categories": [
    "authentication",
    "OAuth2",
    "API security"
  ],
  "source_docs": [
    "7d72497aa7a681bd"
  ],
  "backlinks": null,
  "word_count": 498,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw can act as an OAuth2 authorization server. This means external tools — Claude Desktop, custom MCP clients, third-party integrations — can request delegated access to specific scopes (e.g., `chat`, `sessions`, `memory`) without the user sharing their master token.

The flow follows the standard authorization code grant:

1. Client redirects user to `/oauth/authorize`
2. User sees a consent form and approves or denies
3. Server issues an authorization code and redirects back to the client
4. Client exchanges the code for access + refresh tokens at `/oauth/token`
5. Client calls `/oauth/revoke` when done

## Consent Endpoint

The `GET /oauth/authorize` endpoint renders an inline HTML consent form. The form is embedded as a Python f-string (`_CONSENT_HTML`) so the entire consent flow works without a separate frontend server. This matters for serve mode deployments where the full dashboard may not be running.

Query parameters `client_id`, `redirect_uri`, `scope`, `state`, and `response_type` are forwarded into hidden form fields. The client_id and redirect_uri are validated by the backing `OAuthServer` implementation before the form is shown, preventing open-redirect attacks where a malicious `redirect_uri` would send codes to an attacker-controlled server.

The `POST /oauth/authorize` endpoint (`authorize_consent`) processes the form submission. On approval it delegates to `OAuthServer.authorize()` to generate the code and constructs the redirect URI using `urlencode` — never by string concatenation — so special characters in state tokens cannot break the redirect.

## Token Endpoint

`POST /oauth/token` handles two grant types via `TokenRequest`:

- **`authorization_code`** — exchanges the short-lived code for an access token and a refresh token
- **`refresh_token`** — rotates the refresh token and issues a new access token

Using separate cases rather than a single generic handler ensures each grant type applies only the validations relevant to it. A refresh token cannot be used where a code is expected and vice versa.

The response is a `JSONResponse` rather than a Pydantic model because the OAuth2 token response format is specified by RFC 6749 and must include `token_type: "Bearer"` at the top level — fitting it into a rigid Pydantic schema would require extra translation.

## Revoke Endpoint

`POST /oauth/revoke` accepts either an access or refresh token and calls `OAuthServer.revoke_token()`. Per RFC 7009, revocation always returns 200 even if the token was already invalid — this prevents attackers from probing token validity through revocation responses.

## Error Handling

Authorization errors redirect back to the client with `error` and `error_description` query parameters rather than raising HTTP exceptions, which is the OAuth2-compliant behaviour clients expect. Token and revocation errors raise HTTPException because those endpoints return JSON and clients check the HTTP status code directly.

## Known Gaps

- PKCE (`code_challenge` / `code_verifier`) is not implemented. This is the recommended extension for public clients (SPAs, mobile apps) that cannot store a client secret. Without PKCE, public clients are vulnerable to authorization code interception.
- The consent form does not display the list of requested scopes to the user, only the app name. Users cannot make an informed decision about what they are granting.

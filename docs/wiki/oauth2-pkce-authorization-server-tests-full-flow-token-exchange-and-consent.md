---
{
  "title": "OAuth2 PKCE Authorization Server Tests: Full Flow, Token Exchange, and Consent",
  "summary": "Tests PocketPaw's OAuth2 authorization server with PKCE (Proof Key for Code Exchange), covering authorization code issuance, token exchange, token refresh, revocation, and the consent UI endpoints. The tests validate both the server logic class directly and the REST endpoints, ensuring the security guarantees of PKCE are enforced end-to-end.",
  "concepts": [
    "OAuth2",
    "PKCE",
    "code_verifier",
    "code_challenge",
    "authorization code",
    "token exchange",
    "token refresh",
    "token revocation",
    "consent flow",
    "redirect URI validation",
    "single-use enforcement",
    "AuthorizationServer"
  ],
  "categories": [
    "testing",
    "authentication",
    "OAuth2",
    "security",
    "test"
  ],
  "source_docs": [
    "0fac94ada3a4cdd5"
  ],
  "backlinks": null,
  "word_count": 463,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_api_oauth2.py` covers PocketPaw's OAuth2 implementation. PocketPaw supports OAuth2 so that third-party applications and agent runners can request delegated access on behalf of a user without handling the user's credentials directly. PKCE (Proof Key for Code Exchange) is required for all authorization flows to prevent authorization code interception attacks.

## PKCE Utility

`_make_pkce_pair()` is a module-level helper that generates a `code_verifier` (random URL-safe string) and its SHA-256 `code_challenge` (base64url-encoded). Both unit and endpoint tests call this helper to produce valid PKCE pairs, ensuring the tests use the same algorithm the real client would use.

## Authorization Server Unit Tests

`TestAuthorizationServer` tests `AuthorizationServer` directly with an in-memory `OAuthStorage`, bypassing HTTP. This isolation allows precise assertion on error conditions without the noise of HTTP status code mapping.

### Authorization Code Issuance

`test_authorize_creates_code` verifies the server issues a code given valid client ID, redirect URI, and PKCE challenge. Three complementary tests cover failure cases: invalid client, invalid redirect URI (a critical security check — a mismatched redirect would allow authorization code theft), and invalid scope.

**Why redirect URI validation matters:** OAuth2 authorization code theft attacks work by injecting a malicious `redirect_uri` that sends the code to the attacker. The server must reject any redirect URI not pre-registered for the client.

### Token Exchange

`test_exchange_with_valid_verifier` proves a code plus the matching `code_verifier` produces an access token. `test_exchange_invalid_verifier` confirms a mismatched verifier (correct format but wrong value) is rejected. `test_exchange_invalid_code` verifies expired or fabricated codes fail. `test_exchange_code_reuse` is the single-use enforcement test — each authorization code must be invalidated after first use to prevent replay attacks.

### Token Refresh and Revocation

`test_refresh_token` verifies a refresh token produces a new access token. `test_refresh_invalid_token` confirms fabricated refresh tokens are rejected. `test_revoke_access_token` and `test_revoke_refresh_token` ensure both token types can be invalidated. `test_verify_access_token` confirms a valid access token passes the verify check (used by middleware to authenticate API requests).

## OAuth2 Endpoint Tests

`TestOAuth2Endpoints` mounts the router on a test FastAPI app and exercises all flows over HTTP.

### Consent Flow

`test_authorize_shows_consent` verifies the authorization endpoint returns a consent page (HTML) rather than immediately issuing a code — user consent must be explicit. `test_consent_deny` and `test_consent_allow` cover the consent form submission paths: deny should redirect with an error code, allow should redirect with a valid authorization code.

### Full Token Flow

`test_token_exchange_full_flow` is the end-to-end integration test: authorize → consent allow → extract code from redirect → exchange for token. `test_token_refresh` extends this to test refresh token use. `test_token_exchange_invalid_code` tests the HTTP error response (400) for a bad code. `test_revoke_endpoint` verifies the revocation REST endpoint works correctly.

## Known Gaps

No TODO or FIXME markers. The test suite does not cover token expiry (access token timeout), concurrent code exchanges (race condition on single-use enforcement), or the behavior when `OAuthStorage` persistence fails.
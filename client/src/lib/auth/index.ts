export { generateCodeVerifier, generateCodeChallenge, generateState } from "./pkce";
export { readTokens, saveTokens, clearTokens, type OAuthTokens } from "./token-store";
export { startOAuthFlow, revokeTokens, type OAuthResult } from "./oauth-flow";
export {
  refreshAccessToken,
  scheduleTokenRefresh,
  cancelScheduledRefresh,
} from "./token-refresh";

/** Check if running inside a Tauri webview */
export function isTauri(): boolean {
  return typeof window !== "undefined" && !!(window as any).__TAURI_INTERNALS__;
}

/**
 * Get a valid access token. Tries stored tokens first, refreshes if expired,
 * returns null if no valid token is available (caller should trigger OAuth flow).
 */
export async function getValidToken(): Promise<string | null> {
  const { readTokens } = await import("./token-store");
  const tokens = await readTokens();
  if (!tokens) return null;

  const nowS = Math.floor(Date.now() / 1000);

  // Still valid (with 30s buffer)
  if (tokens.expires_at > nowS + 30) {
    return tokens.access_token;
  }

  // Try refreshing
  if (tokens.refresh_token) {
    try {
      const { refreshAccessToken } = await import("./token-refresh");
      const newTokens = await refreshAccessToken(tokens);
      return newTokens.access_token;
    } catch {
      return null;
    }
  }

  return null;
}

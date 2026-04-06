// Token refresh logic with deduplication and scheduled auto-refresh

import { saveTokens, type OAuthTokens } from "./token-store";
import { API_BASE } from "$lib/api/config";
const REFRESH_MARGIN_S = 5 * 60; // refresh 5 minutes before expiry

let refreshPromise: Promise<OAuthTokens> | null = null;
let scheduledTimer: ReturnType<typeof setTimeout> | null = null;

export async function refreshAccessToken(tokens: OAuthTokens): Promise<OAuthTokens> {
  // Deduplicate concurrent refresh calls
  if (refreshPromise) return refreshPromise;

  refreshPromise = doRefresh(tokens).finally(() => {
    refreshPromise = null;
  });

  return refreshPromise;
}

async function doRefresh(tokens: OAuthTokens): Promise<OAuthTokens> {
  if (!tokens.refresh_token) {
    throw new Error("No refresh token available");
  }

  const res = await fetch(`${API_BASE}/oauth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      grant_type: "refresh_token",
      refresh_token: tokens.refresh_token,
      client_id: "pocketpaw-desktop",
    }),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Refresh failed: HTTP ${res.status}: ${text}`);
  }

  const data = await res.json();

  const newTokens: OAuthTokens = {
    access_token: data.access_token,
    refresh_token: data.refresh_token ?? tokens.refresh_token,
    expires_at: Math.floor(Date.now() / 1000) + (data.expires_in ?? 3600),
    scopes: data.scope ? data.scope.split(" ") : tokens.scopes,
  };

  await saveTokens(newTokens);
  return newTokens;
}

export function scheduleTokenRefresh(
  tokens: OAuthTokens,
  onRefreshed: (newTokens: OAuthTokens) => void,
  onFailed: (error: Error) => void,
): void {
  cancelScheduledRefresh();

  const nowS = Math.floor(Date.now() / 1000);
  const delayS = Math.max(tokens.expires_at - nowS - REFRESH_MARGIN_S, 10);
  const delayMs = delayS * 1000;

  scheduledTimer = setTimeout(async () => {
    try {
      const newTokens = await refreshAccessToken(tokens);
      onRefreshed(newTokens);
      // Schedule the next refresh
      scheduleTokenRefresh(newTokens, onRefreshed, onFailed);
    } catch (err) {
      onFailed(err instanceof Error ? err : new Error(String(err)));
    }
  }, delayMs);
}

export function cancelScheduledRefresh(): void {
  if (scheduledTimer) {
    clearTimeout(scheduledTimer);
    scheduledTimer = null;
  }
}

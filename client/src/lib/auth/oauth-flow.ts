// Orchestrates the full OAuth 2.0 PKCE flow using system browser + localhost callback server

import { generateCodeVerifier, generateCodeChallenge, generateState } from "./pkce";
import { saveTokens, clearTokens, type OAuthTokens } from "./token-store";
import { API_BASE } from "$lib/api/config";
const CLIENT_ID = "pocketpaw-desktop";
const SCOPES = "admin";
const FLOW_TIMEOUT_MS = 5 * 60 * 1000; // 5 minutes

export interface OAuthResult {
  success: boolean;
  tokens?: OAuthTokens;
  error?: string;
}

export async function startOAuthFlow(): Promise<OAuthResult> {
  const verifier = generateCodeVerifier();
  const challenge = await generateCodeChallenge(verifier);
  const state = generateState();

  const { listen } = await import("@tauri-apps/api/event");
  const { invoke } = await import("@tauri-apps/api/core");
  const { openUrl } = await import("@tauri-apps/plugin-opener");

  // Start the temporary localhost server and get the port
  let port: number;
  try {
    port = await invoke<number>("start_oauth_server");
  } catch (err) {
    return { success: false, error: `Failed to start OAuth server: ${err}` };
  }

  const redirectUri = `http://localhost:${port}`;

  const authorizeUrl = new URL(`${API_BASE}/oauth/authorize`);
  authorizeUrl.searchParams.set("client_id", CLIENT_ID);
  authorizeUrl.searchParams.set("redirect_uri", redirectUri);
  authorizeUrl.searchParams.set("response_type", "code");
  authorizeUrl.searchParams.set("scope", SCOPES);
  authorizeUrl.searchParams.set("code_challenge", challenge);
  authorizeUrl.searchParams.set("code_challenge_method", "S256");
  authorizeUrl.searchParams.set("state", state);

  return new Promise<OAuthResult>((resolve) => {
    let settled = false;
    let unlistenRedirect: (() => void) | null = null;
    let timeoutId: ReturnType<typeof setTimeout> | null = null;

    function cleanup() {
      unlistenRedirect?.();
      if (timeoutId) clearTimeout(timeoutId);
    }

    function settle(result: OAuthResult) {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(result);
    }

    // Timeout fallback
    timeoutId = setTimeout(() => {
      settle({ success: false, error: "Sign-in timed out. Please try again." });
    }, FLOW_TIMEOUT_MS);

    // Listen for the redirect URL from the Rust localhost server
    listen<string>("oauth-redirect", async (event) => {
      if (settled) return;

      try {
        const callbackUrl = new URL(event.payload);
        const code = callbackUrl.searchParams.get("code");
        const returnedState = callbackUrl.searchParams.get("state");

        if (!code) {
          const error = callbackUrl.searchParams.get("error") || "No authorization code received.";
          settle({ success: false, error });
          return;
        }

        if (returnedState !== state) {
          settle({ success: false, error: "State mismatch — possible CSRF attack." });
          return;
        }

        // Mark settled before async work to prevent races
        settled = true;
        cleanup();

        const tokens = await exchangeCodeForTokens(code, verifier, redirectUri);
        await saveTokens(tokens);
        resolve({ success: true, tokens });
      } catch (err) {
        settle({ success: false, error: `Token exchange failed: ${err}` });
      }
    }).then((unlisten) => {
      unlistenRedirect = unlisten;
    });

    // Open the OAuth URL in the system browser
    openUrl(authorizeUrl.toString()).catch((err) => {
      settle({ success: false, error: `Failed to open browser: ${err}` });
    });
  });
}

async function exchangeCodeForTokens(
  code: string,
  codeVerifier: string,
  redirectUri: string,
): Promise<OAuthTokens> {
  const res = await fetch(`${API_BASE}/oauth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      grant_type: "authorization_code",
      code,
      code_verifier: codeVerifier,
      client_id: CLIENT_ID,
      redirect_uri: redirectUri,
    }),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status}: ${text}`);
  }

  const data = await res.json();

  return {
    access_token: data.access_token,
    refresh_token: data.refresh_token ?? null,
    expires_at: Math.floor(Date.now() / 1000) + (data.expires_in ?? 3600),
    scopes: data.scope ? data.scope.split(" ") : [],
  };
}

export async function revokeTokens(accessToken: string): Promise<void> {
  try {
    await fetch(`${API_BASE}/oauth/revoke`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: accessToken }),
    });
  } catch {
    // Best-effort revocation
  }
  await clearTokens();
}

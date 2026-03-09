import { PocketPawClient, PocketPawWebSocket, type ConnectionState } from "$lib/api";
import { BACKEND_URL } from "$lib/api/config";

class ConnectionStore {
  status = $state<ConnectionState>("disconnected");
  token = $state<string | null>(null);
  error = $state<string | null>(null);
  backendUrl = $state(BACKEND_URL);

  isConnected = $derived(this.status === "connected");

  private ws: PocketPawWebSocket | null = null;
  private client: PocketPawClient | null = null;
  private unsubState: (() => void) | null = null;

  async initialize(token: string, baseUrl?: string, wsToken?: string): Promise<void> {
    this.disconnect();

    const url = baseUrl ?? BACKEND_URL;
    this.backendUrl = url;
    this.token = token;
    this.error = null;

    // Create REST client (uses OAuth token in Authorization header)
    this.client = new PocketPawClient(url, token);

    // Exchange the token for a session cookie via the login endpoint.
    // The WebSocket handler validates this cookie, avoiding the need to
    // pass tokens in the URL (which the HTTP auth middleware would reject).
    const effectiveWsToken = wsToken ?? token;
    try {
      await this.client.loginForSession(effectiveWsToken);
    } catch {
      // Non-fatal — WS may still connect via other auth methods
    }

    // Create WebSocket client (no token in URL — rely on session cookie)
    this.ws = new PocketPawWebSocket(this.client.getWsUrl());

    // Mirror WS connection state into this store
    this.unsubState = this.ws.onStateChange((state) => {
      this.status = state;
      if (state === "connected") {
        this.error = null;
      }
    });

    // Track errors
    this.ws.on("error", (event) => {
      if (event.type === "error") {
        this.error = event.content;
      }
    });

    // Connect
    this.ws.connect();
  }

  disconnect(): void {
    this.unsubState?.();
    this.unsubState = null;
    this.ws?.disconnect();
    this.ws = null;
    this.client = null;
    this.status = "disconnected";
  }

  getClient(): PocketPawClient {
    if (!this.client) {
      throw new Error("PocketPawClient not initialized. Call initialize() first.");
    }
    return this.client;
  }

  getWebSocket(): PocketPawWebSocket {
    if (!this.ws) {
      throw new Error("PocketPawWebSocket not initialized. Call initialize() first.");
    }
    return this.ws;
  }

  async updateToken(newToken: string): Promise<void> {
    this.token = newToken;
    this.client?.setToken(newToken);
    // Refresh session cookie before reconnecting WebSocket
    try {
      await this.client?.loginForSession(newToken);
    } catch {
      // Non-fatal
    }
    this.ws?.reconnectWithToken(newToken);
  }
}

export const connectionStore = new ConnectionStore();

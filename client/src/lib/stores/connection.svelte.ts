import { PocketPawClient, PocketPawWebSocket, type ConnectionState } from "$lib/api";
import { BACKEND_URL } from "$lib/api/config";
import { toast } from "svelte-sonner";

/** Number of consecutive failures before suggesting the backend may be down. */
const FAILURE_THRESHOLD = 5;

class ConnectionStore {
  status = $state<ConnectionState>("disconnected");
  token = $state<string | null>(null);
  error = $state<string | null>(null);
  backendUrl = $state(BACKEND_URL);
  isOffline = $state(false);
  consecutiveFailures = $state(0);

  isConnected = $derived(this.status === "connected");

  private ws: PocketPawWebSocket | null = null;
  private client: PocketPawClient | null = null;
  private unsubState: (() => void) | null = null;
  private wasConnected = false;
  private onlineHandler: (() => void) | null = null;
  private offlineHandler: (() => void) | null = null;

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
      const prev = this.status;
      this.status = state;
      if (state === "connected") {
        this.error = null;
        this.consecutiveFailures = 0;
        // Show reconnection toast if we were previously disconnected (not first connect)
        if (this.wasConnected && prev === "disconnected") {
          toast.success("Reconnected to backend");
        }
        this.wasConnected = true;
      } else if (state === "disconnected" && this.wasConnected) {
        this.consecutiveFailures++;
        if (this.consecutiveFailures === FAILURE_THRESHOLD) {
          toast.error(
            "Unable to reach the backend after multiple attempts. Make sure it's running.",
            { duration: 10000 },
          );
        }
      }
    });

    // Track errors
    this.ws.on("error", (event) => {
      if (event.type === "error") {
        this.error = event.content;
      }
    });

    // Offline detection
    this.cleanupOfflineListeners();
    this.isOffline = typeof navigator !== "undefined" && !navigator.onLine;
    this.onlineHandler = () => {
      this.isOffline = false;
      toast.success("Network connection restored");
      // Trigger reconnect if WS is disconnected
      if (this.ws && this.status === "disconnected") {
        this.ws.connect();
      }
    };
    this.offlineHandler = () => {
      this.isOffline = true;
      toast.warning("You're offline. Reconnecting when network is available.");
    };
    if (typeof window !== "undefined") {
      window.addEventListener("online", this.onlineHandler);
      window.addEventListener("offline", this.offlineHandler);
    }

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
    this.cleanupOfflineListeners();
  }

  private cleanupOfflineListeners(): void {
    if (typeof window !== "undefined") {
      if (this.onlineHandler) window.removeEventListener("online", this.onlineHandler);
      if (this.offlineHandler) window.removeEventListener("offline", this.offlineHandler);
    }
    this.onlineHandler = null;
    this.offlineHandler = null;
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

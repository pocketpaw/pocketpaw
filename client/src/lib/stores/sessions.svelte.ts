import type { Session } from "$lib/api";
import { toast } from "svelte-sonner";
import { connectionStore } from "./connection.svelte";
import { chatStore } from "./chat.svelte";

const STORAGE_KEY = "pocketpaw_active_session";
const PINNED_KEY = "pocketpaw_pinned_sessions";

class SessionStore {
  sessions = $state<Session[]>([]);
  activeSessionId = $state<string | null>(null);
  isLoading = $state(false);
  isLoadingHistory = $state(false);
  pinnedSessionIds = $state<Set<string>>(new Set());

  activeSession = $derived(
    this.sessions.find((s) => s.id === this.activeSessionId) ?? null,
  );

  pinnedSessions = $derived(
    this.sessions.filter((s) => this.pinnedSessionIds.has(s.id)),
  );

  constructor() {
    try {
      const raw = localStorage.getItem(PINNED_KEY);
      if (raw) {
        this.pinnedSessionIds = new Set(JSON.parse(raw));
      }
    } catch {
      // Ignore
    }
  }

  togglePin(sessionId: string): void {
    const next = new Set(this.pinnedSessionIds);
    if (next.has(sessionId)) {
      next.delete(sessionId);
    } else {
      next.add(sessionId);
    }
    this.pinnedSessionIds = next;
    try {
      localStorage.setItem(PINNED_KEY, JSON.stringify([...next]));
    } catch {
      // localStorage unavailable
    }
  }

  isSessionPinned(sessionId: string): boolean {
    return this.pinnedSessionIds.has(sessionId);
  }

  /** Set the active session ID and persist to localStorage. */
  setActiveSession(id: string | null): void {
    this.activeSessionId = id;
    try {
      if (id) {
        localStorage.setItem(STORAGE_KEY, id);
      } else {
        localStorage.removeItem(STORAGE_KEY);
      }
    } catch {
      // localStorage may be unavailable (e.g. private browsing)
    }
  }

  /** Read the persisted session ID from localStorage (returns null if absent). */
  private getSavedSessionId(): string | null {
    try {
      return localStorage.getItem(STORAGE_KEY);
    } catch {
      return null;
    }
  }

  async loadSessions(limit = 50): Promise<void> {
    this.isLoading = true;
    try {
      const client = connectionStore.getClient();
      const res = await client.listSessions(limit);
      this.sessions = res.sessions;

      // Restore the last active session on page load
      if (!this.activeSessionId && this.sessions.length > 0) {
        const savedId = this.getSavedSessionId();
        const target = savedId && this.sessions.find((s) => s.id === savedId)
          ? savedId
          : this.sessions[0].id;
        this.activeSessionId = target;
        try {
          const history = await client.getSessionHistory(target);
          chatStore.loadHistory(history);
        } catch {
          // Session might be empty or deleted — ignore
        }
        this.setActiveSession(target);
      }
    } catch (err) {
      console.error("[SessionStore] Failed to load sessions:", err);
      toast.error("Failed to load sessions");
    } finally {
      this.isLoading = false;
    }
  }

  async switchSession(sessionId: string): Promise<void> {
    if (sessionId === this.activeSessionId) return;

    this.setActiveSession(sessionId);
    this.isLoadingHistory = true;

    try {
      const client = connectionStore.getClient();
      const history = await client.getSessionHistory(sessionId);
      chatStore.loadHistory(history);
    } catch (err) {
      console.error("[SessionStore] Failed to load session history:", err);
    } finally {
      this.isLoadingHistory = false;
    }
  }

  async createNewSession(): Promise<void> {
    chatStore.clearMessages();

    try {
      const client = connectionStore.getClient();
      const res = await client.createSession();
      this.setActiveSession(res.id);

      // Prepend a placeholder session entry
      const newSession: Session = {
        id: res.id,
        title: res.title,
        channel: "websocket",
        last_activity: new Date().toISOString(),
        message_count: 0,
      };
      this.sessions = [newSession, ...this.sessions];
    } catch {
      // Fallback: clear session ID — first chat will auto-create via stream_end.session_id
      this.setActiveSession(null);
    }
  }

  async deleteSession(sessionId: string): Promise<void> {
    try {
      const client = connectionStore.getClient();
      await client.deleteSession(sessionId);

      this.sessions = this.sessions.filter((s) => s.id !== sessionId);

      // If we deleted the active session, switch to the most recent
      if (this.activeSessionId === sessionId) {
        const next = this.sessions[0];
        if (next) {
          await this.switchSession(next.id);
        } else {
          this.setActiveSession(null);
          chatStore.clearMessages();
        }
      }
    } catch (err) {
      console.error("[SessionStore] Failed to delete session:", err);
      toast.error("Failed to delete session");
    }
  }

  async renameSession(sessionId: string, title: string): Promise<void> {
    try {
      const client = connectionStore.getClient();
      await client.updateSessionTitle(sessionId, title);

      // Update local state
      const session = this.sessions.find((s) => s.id === sessionId);
      if (session) {
        session.title = title;
      }
    } catch (err) {
      console.error("[SessionStore] Failed to rename session:", err);
      toast.error("Failed to rename session");
    }
  }

  async searchSessions(query: string): Promise<Session[]> {
    try {
      const client = connectionStore.getClient();
      return await client.searchSessions(query);
    } catch (err) {
      console.error("[SessionStore] Failed to search sessions:", err);
      return [];
    }
  }

  async exportSession(
    sessionId: string,
    format: "json" | "md" = "json",
  ): Promise<string> {
    const client = connectionStore.getClient();
    return client.exportSession(sessionId, format);
  }
}

export const sessionStore = new SessionStore();

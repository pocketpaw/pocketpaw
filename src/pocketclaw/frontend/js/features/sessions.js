/**
 * PocketPaw - Sessions Feature Module
 *
 * Created: 2026-02-10
 *
 * Session-aware chat: sidebar session list, switching, new chat,
 * delete, rename, grouped display (Today/Yesterday/This Week/Older).
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.Sessions = {
    name: 'Sessions',
    getState() {
        return {
            sessions: [],
            currentSessionId: null,
            sessionsLoading: false,
            sessionsTotal: 0,
            sessionSearch: '',
            sessionsCollapsed: false,
            editingSessionId: null,
            editingSessionTitle: ''
        };
    },

    getMethods() {
        return {
            /**
             * Load sessions from the server
             */
            async loadSessions() {
                this.sessionsLoading = true;
                try {
                    const res = await fetch('/api/sessions?limit=100');
                    if (res.ok) {
                        const data = await res.json();
                        this.sessions = data.sessions || [];
                        this.sessionsTotal = data.total || 0;
                    }
                } catch (e) {
                    console.error('[Sessions] Failed to load:', e);
                } finally {
                    this.sessionsLoading = false;
                }
            },

            /**
             * Select and switch to a session
             */
            selectSession(id) {
                if (this.currentSessionId === id) return;

                // Cache current messages before switching
                if (this.currentSessionId && this.messages.length > 0) {
                    StateManager.cacheSession(this.currentSessionId, this.messages);
                }

                this.currentSessionId = id;
                StateManager.save('lastSession', id);

                // Clear streaming state from previous session
                if (this.isStreaming) {
                    if (this._streamTimeout) {
                        clearTimeout(this._streamTimeout);
                        this._streamTimeout = null;
                    }
                    this.isStreaming = false;
                    this.isThinking = false;
                    this.streamingContent = '';
                }

                // Check cache first for instant display
                const cached = StateManager.getCachedSession(id);
                if (cached) {
                    this.messages = cached;
                }

                // Always fetch from server to get latest
                socket.send('switch_session', { session_id: id });

                // Close mobile sidebar
                this.sidebarOpen = false;
            },

            /**
             * Create a new chat session
             */
            createNewChat() {
                // Cache current messages
                if (this.currentSessionId && this.messages.length > 0) {
                    StateManager.cacheSession(this.currentSessionId, this.messages);
                }

                this.messages = [];
                this.currentSessionId = null;
                this.isStreaming = false;
                this.streamingContent = '';
                StateManager.remove('lastSession');

                socket.send('new_session');
                this.sidebarOpen = false;
            },

            /**
             * Delete a session
             */
            async deleteSession(id, event) {
                if (event) event.stopPropagation();

                try {
                    const res = await fetch(`/api/sessions/${id}`, { method: 'DELETE' });
                    if (res.ok) {
                        this.sessions = this.sessions.filter(s => s.id !== id);
                        this.sessionsTotal = Math.max(0, this.sessionsTotal - 1);
                        StateManager.invalidateSession(id);

                        // If deleting current session, create new chat
                        if (this.currentSessionId === id) {
                            this.createNewChat();
                        }
                    }
                } catch (e) {
                    console.error('[Sessions] Delete failed:', e);
                }
            },

            /**
             * Start inline rename
             */
            startRenameSession(id, title, event) {
                if (event) event.stopPropagation();
                this.editingSessionId = id;
                this.editingSessionTitle = title;
                this.$nextTick(() => {
                    const input = document.querySelector('.session-rename-input');
                    if (input) { input.focus(); input.select(); }
                });
            },

            /**
             * Save renamed session
             */
            async saveRenameSession(id) {
                const title = this.editingSessionTitle.trim();
                if (!title) {
                    this.editingSessionId = null;
                    return;
                }

                try {
                    const res = await fetch(`/api/sessions/${id}/title`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ title })
                    });
                    if (res.ok) {
                        const session = this.sessions.find(s => s.id === id);
                        if (session) session.title = title;
                    }
                } catch (e) {
                    console.error('[Sessions] Rename failed:', e);
                }
                this.editingSessionId = null;
            },

            /**
             * Cancel inline rename
             */
            cancelRenameSession() {
                this.editingSessionId = null;
            },

            /**
             * Group sessions by time period, filtered by search
             */
            getGroupedSessions() {
                let filtered = this.sessions;

                // Apply search filter
                if (this.sessionSearch.trim()) {
                    const q = this.sessionSearch.toLowerCase();
                    filtered = filtered.filter(s =>
                        (s.title || '').toLowerCase().includes(q) ||
                        (s.preview || '').toLowerCase().includes(q)
                    );
                }

                const now = new Date();
                const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
                const yesterday = new Date(today); yesterday.setDate(yesterday.getDate() - 1);
                const weekAgo = new Date(today); weekAgo.setDate(weekAgo.getDate() - 7);

                const groups = [
                    { label: 'Today', sessions: [] },
                    { label: 'Yesterday', sessions: [] },
                    { label: 'This Week', sessions: [] },
                    { label: 'Older', sessions: [] }
                ];

                for (const s of filtered) {
                    const d = new Date(s.last_activity || s.created || 0);
                    if (d >= today) groups[0].sessions.push(s);
                    else if (d >= yesterday) groups[1].sessions.push(s);
                    else if (d >= weekAgo) groups[2].sessions.push(s);
                    else groups[3].sessions.push(s);
                }

                return groups.filter(g => g.sessions.length > 0);
            },

            /**
             * Map channel name to Lucide icon
             */
            getChannelIcon(channel) {
                const icons = {
                    websocket: 'globe',
                    telegram: 'send',
                    discord: 'gamepad-2',
                    slack: 'hash',
                    whatsapp: 'smartphone',
                    signal: 'shield',
                    matrix: 'grid-3x3',
                    teams: 'users',
                    google_chat: 'message-circle',
                    unknown: 'message-square'
                };
                return icons[channel] || icons.unknown;
            },

            /**
             * Handle session_history message from server
             */
            handleSessionHistory(data) {
                this.currentSessionId = data.session_id;
                const messages = (data.messages || []).map(m => ({
                    role: m.role || 'user',
                    content: m.content || '',
                    time: m.timestamp
                        ? new Date(m.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
                        : '',
                    isNew: false
                }));
                this.messages = messages;
                StateManager.save('lastSession', data.session_id);
                StateManager.cacheSession(data.session_id, messages);

                // Scroll to bottom
                this.$nextTick(() => {
                    const el = this.$refs.messages;
                    if (el) el.scrollTop = el.scrollHeight;
                });
            },

            /**
             * Handle new_session message from server
             */
            handleNewSession(data) {
                this.currentSessionId = data.id;
                this.messages = [];
                StateManager.save('lastSession', data.id);
            },

            /**
             * Auto-title: update session in sidebar after first response
             */
            autoTitleCurrentSession() {
                if (!this.currentSessionId) return;

                // Find first user message
                const firstUserMsg = this.messages.find(m => m.role === 'user');
                if (!firstUserMsg) return;

                const title = firstUserMsg.content.substring(0, 80);
                const session = this.sessions.find(s => s.id === this.currentSessionId);
                if (session && (!session.title || session.title === 'New Chat')) {
                    session.title = title;
                }
            }
        };
    }
};

window.PocketPaw.Loader.register('Sessions', window.PocketPaw.Sessions);

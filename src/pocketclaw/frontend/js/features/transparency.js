/**
 * PocketPaw - Transparency Feature Module
 *
 * Created: 2026-02-05
 * Extracted from app.js as part of componentization refactor.
 *
 * Contains transparency panel features:
 * - Identity panel
 * - Memory panel (sessions + long-term)
 * - Audit logs
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.Transparency = {
    /**
     * Get initial state for Transparency features
     */
    getState() {
        return {
            // Identity
            showIdentity: false,
            identityLoading: false,
            identityData: {},

            // Memory
            showMemory: false,
            memoryTab: 'sessions',  // 'sessions', 'long_term'
            sessionsList: [],       // List of all sessions
            sessionMemory: [],      // Current session history
            selectedSession: null,  // Currently selected session ID
            longTermMemory: [],
            memoryLoading: false,
            memorySearch: '',
            memoryStats: { total_memories: 0, active_context: 0, archived: 0, user_interactions: 0 },

            // Audit
            showAudit: false,
            auditLoading: false,
            auditLogs: [],

            // Activity log (for system events)
            activityLog: [],
            sessionId: null
        };
    },

    /**
     * Get methods for Transparency features
     */
    getMethods() {
        return {
            // ==================== Connection Info ====================

            /**
             * Handle connection info (capture session ID)
             */
            handleConnectionInfo(data) {
                this.handleNotification(data);
                if (data.id) {
                    this.sessionId = data.id;
                    this.log(`Session ID: ${data.id}`, 'info');
                }
            },

            /**
             * Handle system event (Activity Log + Mission Control events)
             */
            handleSystemEvent(data) {
                const time = Tools.formatTime();
                const eventType = data.event_type || '';

                // Handle Mission Control events
                if (eventType.startsWith('mc_')) {
                    this.handleMCEvent(data);
                    return;
                }

                // Handle standard system events
                let message = '';
                let level = 'info';

                if (eventType === 'thinking') {
                    message = `<span class="text-accent animate-pulse">Thinking...</span>`;
                } else if (eventType === 'tool_start') {
                    message = `🔧 <b>${data.data.name}</b> <span class="text-white/50">${JSON.stringify(data.data.params)}</span>`;
                    level = 'warning';
                } else if (eventType === 'tool_result') {
                    const isError = data.data.status === 'error';
                    level = isError ? 'error' : 'success';
                    message = `${isError ? '❌' : '✅'} <b>${data.data.name}</b> result: <span class="text-white/50">${String(data.data.result).substring(0, 50)} ${(String(data.data.result).length > 50) ? '...' : ''}</span>`;
                } else {
                    message = `Unknown event: ${eventType}`;
                }

                this.activityLog.push({ time, message, level });

                // Auto-scroll activity log
                this.$nextTick(() => {
                    const term = this.$refs.activityLog;
                    if (term) term.scrollTop = term.scrollHeight;
                });
            },

            // ==================== Identity Panel ====================

            openIdentity() {
                this.showIdentity = true;
                this.identityLoading = true;
                fetch('/api/identity')
                    .then(r => r.json())
                    .then(data => {
                        this.identityData = data;
                        this.identityLoading = false;
                    })
                    .catch(e => {
                        this.showToast('Failed to load identity', 'error');
                        this.identityLoading = false;
                    });
            },

            // ==================== Memory Panel ====================

            openMemory() {
                this.showMemory = true;
                this.memoryLoading = true;
                this.loadSessionsList();
                this.loadLongTermMemory();
            },

            loadSessionsList() {
                fetch('/api/memory/sessions')
                    .then(r => r.json())
                    .then(data => {
                        this.sessionsList = data;
                        this.updateMemoryStats();

                        // Auto-select current session if in list
                        if (this.sessionId && data.some(s => s.id === this.sessionId)) {
                            this.selectSession(this.sessionId);
                        }
                    })
                    .catch(e => {
                        console.error('Failed to load sessions:', e);
                    });
            },

            selectSession(sessionId) {
                this.selectedSession = sessionId;
                fetch(`/api/memory/session?id=${sessionId}`)
                    .then(r => r.json())
                    .then(data => {
                        this.sessionMemory = data;
                    });
            },

            exportSession(format) {
                if (!this.selectedSession) {
                    this.showToast('No session selected', 'error');
                    return;
                }
                const url = `/api/memory/session/export?id=${encodeURIComponent(this.selectedSession)}&format=${format}`;
                const a = document.createElement('a');
                a.href = url;
                a.download = '';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                this.showToast(`Exporting session as ${format.toUpperCase()}`, 'success');
            },

            loadLongTermMemory() {
                fetch('/api/memory/long_term')
                    .then(r => r.json())
                    .then(data => {
                        this.longTermMemory = data;
                        this.updateMemoryStats();
                        this.memoryLoading = false;
                    })
                    .catch(e => {
                        console.error('Failed to load memories:', e);
                        this.memoryLoading = false;
                    });
            },

            updateMemoryStats() {
                const totalSessionMessages = this.sessionsList.reduce((sum, s) => sum + (s.message_count || 0), 0);
                this.memoryStats = {
                    total_memories: this.longTermMemory.length + totalSessionMessages,
                    active_sessions: this.sessionsList.length,
                    long_term_facts: this.longTermMemory.length,
                    user_interactions: this.sessionMemory.filter(m => m.role === 'user').length
                };
            },

            /**
             * Get filtered memories based on search query
             */
            getFilteredMemories() {
                const search = this.memorySearch.toLowerCase().trim();
                const allMemories = [
                    ...this.longTermMemory.map(m => ({ ...m, type: 'long_term', id: m.timestamp })),
                    ...this.sessionMemory.map((m, i) => ({ ...m, type: 'session', id: `session-${i}`, created_at: new Date().toISOString() }))
                ];

                if (!search) return allMemories;

                return allMemories.filter(m =>
                    m.content?.toLowerCase().includes(search) ||
                    m.tags?.some(t => t.toLowerCase().includes(search))
                );
            },

            /**
             * Get CSS class for memory type badge
             */
            getMemoryTypeClass(type) {
                const classes = {
                    'long_term': 'bg-purple-500/20 text-purple-400 border-purple-500/30',
                    'session': 'bg-blue-500/20 text-blue-400 border-blue-500/30',
                    'daily': 'bg-green-500/20 text-green-400 border-green-500/30'
                };
                return classes[type] || 'bg-gray-500/20 text-gray-400 border-gray-500/30';
            },

            /**
             * Format date for display
             */
            formatDate(dateStr) {
                if (!dateStr) return '';
                try {
                    const date = new Date(dateStr);
                    return date.toLocaleDateString('en-US', {
                        month: 'short',
                        day: 'numeric',
                        hour: '2-digit',
                        minute: '2-digit'
                    });
                } catch (e) {
                    return dateStr;
                }
            },

            /**
             * Delete a memory (placeholder - needs backend endpoint)
             */
            deleteMemory(id) {
                // TODO: Add backend endpoint for memory deletion
                this.showToast('Memory deletion not yet implemented', 'info');
            },

            // ==================== Audit Panel ====================

            openAudit() {
                this.showAudit = true;
                this.auditLoading = true;
                fetch('/api/audit')
                    .then(r => r.json())
                    .then(data => {
                        this.auditLogs = data;
                        this.auditLoading = false;
                    })
                    .catch(e => {
                        this.showToast('Failed to load audit logs', 'error');
                        this.auditLoading = false;
                    });
            }
        };
    }
};

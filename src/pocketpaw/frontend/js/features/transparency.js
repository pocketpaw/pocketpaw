/**
 * PocketPaw - Transparency Feature Module
 *
 * Created: 2026-02-05
 * Updated: 2026-02-12 — Added dw_ prefix routing for Deep Work events
 *
 * Contains transparency panel features:
 * - Identity panel
 * - Memory panel (long-term facts + config)
 * - Audit logs
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.Transparency = {
    name: 'Transparency',
    /**
     * Get initial state for Transparency features
     */
    getState() {
        return {
            // Identity
            showIdentity: false,
            identityLoading: false,
            identityData: {},
            identityEditing: false,
            identitySaving: false,
            identityDraft: {},
            identityTab: 'identity_file',
            identityTabs: [
                {
                    key: 'identity_file', label: 'Identity', file: 'IDENTITY.md',
                    icon: 'fingerprint',
                    hint: 'The primary directive — defines who the agent is and how it behaves.',
                },
                {
                    key: 'soul_file', label: 'Soul', file: 'SOUL.md',
                    icon: 'heart',
                    hint: 'Core philosophy and values that guide the agent\'s decisions.',
                },
                {
                    key: 'style_file', label: 'Style', file: 'STYLE.md',
                    icon: 'palette',
                    hint: 'Communication style — tone, formatting, and interaction patterns.',
                },
                {
                    key: 'user_file', label: 'User Profile', file: 'USER.md',
                    icon: 'user',
                    hint: 'Your profile — injected into every prompt so the agent knows you.',
                },
            ],

            // Memory
            showMemory: false,
            longTermMemory: [],
            memoryLoading: false,
            memorySearch: '',
            memoryConfigOpen: false,

            // Audit
            showAudit: false,
            auditLoading: false,
            auditLogs: [],
            auditFilter: '',              // text search
            auditSeverityFilter: 'all',   // 'all' | 'info' | 'warning' | 'alert' | 'critical'

            // Activity log (for system events)
            activityLog: [],
            sessionId: null,

            // Security audit
            securityAuditResults: null,
            securityAuditLoading: false,

            // Self-audit
            selfAuditReports: [],
            selfAuditDetail: null,
            selfAuditRunning: false
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

                // Handle Deep Work events
                if (eventType.startsWith('dw_')) {
                    this.handleDWEvent(data);
                    return;
                }

                // Handle live audit entries
                if (eventType === 'audit_entry') {
                    if (this.showAudit && data.data) {
                        this.auditLogs.unshift(data.data);
                    }
                    return;
                }

                // Handle standard system events
                let message = '';
                let level = 'info';

                if (eventType === 'thinking') {
                    if (data.data && data.data.content) {
                        const rawSnippet = data.data.content.substring(0, 120);
                        const ellipsis = data.data.content.length > 120 ? '...' : '';
                        const snippet = rawSnippet.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
                        message = `<span class="text-white/40 italic">${snippet}${ellipsis}</span>`;
                    } else {
                        message = `<span class="text-accent animate-pulse">Thinking...</span>`;
                    }
                } else if (eventType === 'thinking_done') {
                    message = `<span class="text-white/40">Thinking complete</span>`;
                } else if (eventType === 'tool_start') {
                    const toolName = (data.data.name || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
                    const toolParams = JSON.stringify(data.data.params || {}).replace(/&/g, '&amp;').replace(/</g, '&lt;');
                    message = `🔧 <b>${toolName}</b> <span class="text-white/50">${toolParams}</span>`;
                    level = 'warning';
                } else if (eventType === 'tool_result') {
                    const isError = data.data.status === 'error';
                    level = isError ? 'error' : 'success';
                    const rName = (data.data.name || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
                    const rStr = String(data.data.result || '').substring(0, 50).replace(/&/g, '&amp;').replace(/</g, '&lt;');
                    const rMore = String(data.data.result || '').length > 50 ? '...' : '';
                    message = `${isError ? '❌' : '✅'} <b>${rName}</b> result: <span class="text-white/50">${rStr}${rMore}</span>`;
                } else if (eventType === 'token_usage') {
                    const d = data.data || {};
                    const inp = d.input_tokens || 0;
                    const out = d.output_tokens || 0;
                    const total = d.total_tokens || (inp + out);
                    message = `<span class="text-white/40">Tokens: <b>${inp.toLocaleString()}</b> in · <b>${out.toLocaleString()}</b> out · <b>${total.toLocaleString()}</b> total</span>`;
                    level = 'info';
                } else {
                    message = `Unknown event: ${eventType}`;
                }

                this.activityLog.push({ time, message, level });

                // Also feed plain-text version into Terminal logs
                if (eventType === 'thinking') {
                    this.log('Thinking...', 'info');
                } else if (eventType === 'token_usage') {
                    const d = data.data || {};
                    this.log(`[TOKENS] ${d.input_tokens || 0} in / ${d.output_tokens || 0} out`, 'info');
                } else if (eventType === 'tool_start') {
                    const name = data.data?.name || 'unknown';
                    const params = JSON.stringify(data.data?.params || {}).substring(0, 80);
                    this.log(`[TOOL] ${name} ${params}`, 'warning');
                } else if (eventType === 'tool_result') {
                    const name = data.data?.name || 'unknown';
                    const isErr = data.data?.status === 'error';
                    const result = String(data.data?.result || '').substring(0, 80);
                    this.log(`[${isErr ? 'ERR' : 'OK'}] ${name}: ${result}`, isErr ? 'error' : 'success');
                }

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
                this.identityEditing = false;
                fetch('/api/identity')
                    .then(r => r.json())
                    .then(data => {
                        this.identityData = data;
                        this.identityLoading = false;
                        this.$nextTick(() => { if (window.refreshIcons) window.refreshIcons(); });
                    })
                    .catch(e => {
                        this.showToast('Failed to load identity', 'error');
                        this.identityLoading = false;
                    });
            },

            startIdentityEdit() {
                this.identityDraft = { ...this.identityData };
                this.identityEditing = true;
                this.$nextTick(() => { if (window.refreshIcons) window.refreshIcons(); });
            },

            cancelIdentityEdit() {
                this.identityEditing = false;
                this.identityDraft = {};
            },

            saveIdentity() {
                this.identitySaving = true;
                fetch('/api/identity', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(this.identityDraft),
                })
                    .then(r => r.json())
                    .then(data => {
                        if (data.ok) {
                            this.identityData = { ...this.identityDraft };
                            this.identityEditing = false;
                            this.identityDraft = {};
                            this.showToast('Identity saved — changes apply on next message', 'success');
                        } else {
                            this.showToast('Failed to save identity', 'error');
                        }
                        this.identitySaving = false;
                        this.$nextTick(() => { if (window.refreshIcons) window.refreshIcons(); });
                    })
                    .catch(() => {
                        this.showToast('Failed to save identity', 'error');
                        this.identitySaving = false;
                    });
            },

            // ==================== Memory Panel ====================

            openMemory() {
                this.showMemory = true;
                this.memoryLoading = true;
                fetch('/api/memory/long_term')
                    .then(r => r.json())
                    .then(data => {
                        this.longTermMemory = data;
                        this.memoryLoading = false;
                        this.$nextTick(() => { if (window.refreshIcons) window.refreshIcons(); });
                    })
                    .catch(e => {
                        console.error('Failed to load memories:', e);
                        this.memoryLoading = false;
                    });
            },

            /**
             * Filter long-term memories by search query
             */
            getFilteredMemories() {
                const search = this.memorySearch.toLowerCase().trim();
                if (!search) return this.longTermMemory;
                return this.longTermMemory.filter(m =>
                    m.content?.toLowerCase().includes(search) ||
                    m.tags?.some(t => t.toLowerCase().includes(search))
                );
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
             * Delete a long-term memory
             */
            deleteMemory(id) {
                fetch(`/api/memory/long_term/${encodeURIComponent(id)}`, { method: 'DELETE' })
                    .then(r => {
                        if (!r.ok) throw new Error('Delete failed');
                        this.longTermMemory = this.longTermMemory.filter(m => m.id !== id);
                        this.showToast('Memory forgotten', 'success');
                    })
                    .catch(() => {
                        this.showToast('Failed to delete memory', 'error');
                    });
            },

            // ==================== Audit Panel ====================

            openAudit() {
                this.showAudit = true;
                this.auditLoading = true;
                this.auditFilter = '';
                this.auditSeverityFilter = 'all';
                fetch('/api/audit')
                    .then(r => r.json())
                    .then(data => {
                        this.auditLogs = Array.isArray(data) ? data : [];
                        this.auditLoading = false;
                    })
                    .catch(() => {
                        this.showToast('Failed to load audit logs', 'error');
                        this.auditLoading = false;
                    });
            },

            /**
             * Filtered audit logs based on search + severity filter
             */
            filteredAuditLogs() {
                let logs = this.auditLogs;
                if (this.auditSeverityFilter !== 'all') {
                    logs = logs.filter(l => l.severity === this.auditSeverityFilter);
                }
                if (this.auditFilter.trim()) {
                    const q = this.auditFilter.toLowerCase();
                    logs = logs.filter(l =>
                        (l.action || '').toLowerCase().includes(q) ||
                        (l.target || '').toLowerCase().includes(q) ||
                        (l.actor || '').toLowerCase().includes(q) ||
                        (l.status || '').toLowerCase().includes(q) ||
                        JSON.stringify(l.context || {}).toLowerCase().includes(q)
                    );
                }
                return logs;
            },

            /**
             * Format audit timestamp as relative date + time
             */
            formatAuditDate(ts) {
                if (!ts) return '';
                const d = new Date(ts);
                if (isNaN(d)) return ts;
                const now = new Date();
                const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
                const entry = new Date(d.getFullYear(), d.getMonth(), d.getDate());
                const diff = Math.round((today - entry) / 86400000);
                const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                if (diff === 0) return `Today ${time}`;
                if (diff === 1) return `Yesterday ${time}`;
                if (diff < 7) return `${d.toLocaleDateString([], { weekday: 'short' })} ${time}`;
                return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + time;
            },

            /**
             * Format audit context as readable string
             */
            formatAuditContext(ctx) {
                if (!ctx || typeof ctx !== 'object') return '';
                const keys = Object.keys(ctx);
                if (keys.length === 0) return '';
                return keys.map(k => {
                    const v = ctx[k];
                    const val = typeof v === 'object' ? JSON.stringify(v) : String(v);
                    return `${k}: ${val}`;
                }).join(' · ');
            },

            /**
             * Get status badge color class
             */
            auditStatusClass(status) {
                const map = {
                    'allow': 'bg-success/20 text-success',
                    'success': 'bg-success/20 text-success',
                    'block': 'bg-danger/20 text-danger',
                    'error': 'bg-danger/20 text-danger',
                    'attempt': 'bg-white/10 text-white/60'
                };
                return map[status] || 'bg-white/10 text-white/50';
            },

            /**
             * Clear audit log
             */
            clearAuditLog() {
                if (!confirm('Clear all audit log entries? This cannot be undone.')) return;
                fetch('/api/audit', { method: 'DELETE' })
                    .then(r => r.json())
                    .then(data => {
                        if (data.ok) {
                            this.auditLogs = [];
                            this.showToast('Audit log cleared', 'success');
                        }
                    })
                    .catch(() => {
                        this.showToast('Failed to clear audit log', 'error');
                    });
            },

            // ==================== Security Audit ====================

            runSecurityAudit() {
                this.securityAuditLoading = true;
                this.securityAuditResults = null;
                fetch('/api/security-audit', { method: 'POST' })
                    .then(r => r.json())
                    .then(data => {
                        this.securityAuditResults = data;
                        this.securityAuditLoading = false;
                    })
                    .catch(() => {
                        this.showToast('Security audit failed', 'error');
                        this.securityAuditLoading = false;
                    });
            },

            // ==================== Self-Audit ====================

            loadSelfAuditReports() {
                fetch('/api/self-audit/reports')
                    .then(r => r.json())
                    .then(data => { this.selfAuditReports = data; })
                    .catch(() => {
                        this.showToast('Failed to load self-audit reports', 'error');
                    });
            },

            viewSelfAuditReport(date) {
                fetch(`/api/self-audit/reports/${encodeURIComponent(date)}`)
                    .then(r => r.json())
                    .then(data => { this.selfAuditDetail = data; })
                    .catch(() => {
                        this.showToast('Failed to load report', 'error');
                    });
            },

            triggerSelfAudit() {
                this.selfAuditRunning = true;
                fetch('/api/self-audit/run', { method: 'POST' })
                    .then(r => r.json())
                    .then(data => {
                        this.selfAuditDetail = data;
                        this.selfAuditRunning = false;
                        this.loadSelfAuditReports();
                        this.showToast(`Self-audit complete: ${data.passed}/${data.total_checks} passed`, 'success');
                    })
                    .catch(() => {
                        this.showToast('Self-audit failed', 'error');
                        this.selfAuditRunning = false;
                    });
            }
        };
    }
};

window.PocketPaw.Loader.register('Transparency', window.PocketPaw.Transparency);

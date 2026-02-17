/**
 * PocketPaw - Health Feature Module
 *
 * Created: 2026-02-17
 * Updated: 2026-02-17
 *   - Auto-refresh health data while modal is open (every 15s)
 *   - Re-fetch full health data (including errors) on WS reconnect
 *   - "Fix Issues" button sends diagnostic prompt to agent via chat
 *   - "Clear Log" button to clear resolved errors when system is healthy
 *   - Better error handling in catch blocks (show connection error)
 *
 * Health engine frontend integration:
 * - Health status indicator (sidebar dot + modal)
 * - Real-time health updates via WebSocket
 * - Error log viewer
 * - Self-fix via agent
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.Health = {
    name: 'Health',

    getState() {
        return {
            healthStatus: 'unknown',   // 'healthy' | 'degraded' | 'unhealthy' | 'unknown'
            healthIssues: [],           // HealthCheckResult[] (non-ok only)
            healthErrors: [],           // ErrorStore entries
            showHealthModal: false,
            healthLoading: false,
            healthLastCheck: null,
            _healthPollTimer: null,     // auto-refresh interval while modal is open
            healthFixing: false,        // true while "Fix Issues" is in progress
            healthClearing: false,      // true while "Clear Log" is in progress
        };
    },

    getMethods() {
        return {
            /**
             * Open health panel — fetch current status + recent errors,
             * start auto-refresh while open.
             */
            openHealthPanel() {
                this.showHealthModal = true;
                this._fetchHealthData();
                this._startHealthPoll();
                this.$nextTick(() => { if (window.refreshIcons) window.refreshIcons(); });
            },

            /**
             * Close health panel — stop auto-refresh.
             */
            closeHealthPanel() {
                this.showHealthModal = false;
                this._stopHealthPoll();
            },

            /**
             * Fetch health status + errors from REST API.
             * Called on open, on reconnect, and by the auto-refresh timer.
             */
            _fetchHealthData() {
                this.healthLoading = true;

                // Fetch health status via REST
                fetch('/api/health')
                    .then(r => {
                        if (!r.ok) throw new Error(`HTTP ${r.status}`);
                        return r.json();
                    })
                    .then(data => {
                        this.healthStatus = data.status || 'unknown';
                        this.healthIssues = data.issues || [];
                        this.healthLastCheck = data.last_check;
                        this.healthLoading = false;
                    })
                    .catch(() => {
                        this.healthLoading = false;
                        // Don't overwrite a known status — only set unknown if we had nothing
                        if (!this.healthLastCheck) {
                            this.healthStatus = 'unknown';
                        }
                    });

                // Fetch recent errors
                fetch('/api/health/errors?limit=20')
                    .then(r => {
                        if (!r.ok) throw new Error(`HTTP ${r.status}`);
                        return r.json();
                    })
                    .then(errors => {
                        this.healthErrors = errors || [];
                    })
                    .catch(() => {});
            },

            /**
             * Start auto-refresh polling while the health modal is open.
             * Polls every 15 seconds so the user sees updates after fixes.
             */
            _startHealthPoll() {
                this._stopHealthPoll();
                this._healthPollTimer = setInterval(() => {
                    if (this.showHealthModal) {
                        this._fetchHealthData();
                    } else {
                        this._stopHealthPoll();
                    }
                }, 15000);
            },

            /**
             * Stop auto-refresh polling.
             */
            _stopHealthPoll() {
                if (this._healthPollTimer) {
                    clearInterval(this._healthPollTimer);
                    this._healthPollTimer = null;
                }
            },

            /**
             * Trigger a full health check (startup + connectivity)
             */
            triggerHealthCheck() {
                this.healthLoading = true;

                fetch('/api/health/check', { method: 'POST' })
                    .then(r => {
                        if (!r.ok) throw new Error(`HTTP ${r.status}`);
                        return r.json();
                    })
                    .then(data => {
                        this.healthStatus = data.status || 'unknown';
                        this.healthIssues = data.issues || [];
                        this.healthLastCheck = data.last_check;
                        this.healthLoading = false;
                        this.showToast(
                            `Health check complete: ${(data.status || 'unknown').toUpperCase()}`,
                            data.status === 'healthy' ? 'success' : 'warning'
                        );
                        // Also refresh errors since a check may record new ones
                        fetch('/api/health/errors?limit=20')
                            .then(r => r.ok ? r.json() : [])
                            .then(errors => { this.healthErrors = errors || []; })
                            .catch(() => {});
                    })
                    .catch(() => {
                        this.healthLoading = false;
                        this.showToast('Health check failed — server may be restarting', 'error');
                    });
            },

            /**
             * Ask the agent to diagnose and fix the current health issues.
             * Composes a prompt from the issue list and sends it as a chat message.
             */
            requestHealthFix() {
                if (!this.healthIssues || this.healthIssues.length === 0) {
                    this.showToast('No issues to fix', 'info');
                    return;
                }

                // Build a diagnostic prompt from the current issues
                const issueLines = this.healthIssues.map(i => {
                    const hint = i.fix_hint ? ` (Hint: ${i.fix_hint})` : '';
                    return `- [${i.status.toUpperCase()}] ${i.name}: ${i.message}${hint}`;
                });

                const prompt = [
                    'The health engine detected the following issues:',
                    '',
                    ...issueLines,
                    '',
                    'Please diagnose and fix these issues. Use the health_check tool to verify after fixing.',
                ].join('\n');

                // Switch to chat view and send the message
                this.healthFixing = true;
                this.showHealthModal = false;
                this._stopHealthPoll();
                this.navigateToView('chat');

                // Small delay so the view switch renders
                this.$nextTick(() => {
                    this.addMessage('user', prompt);
                    this.startStreaming();
                    socket.chat(prompt);
                    this.healthFixing = false;
                });
            },

            /**
             * Handle health_update system event from WebSocket.
             * Also called on reconnect since app.js sends get_health on connect.
             */
            handleHealthUpdate(data) {
                if (data && data.data) {
                    this.healthStatus = data.data.status || 'unknown';
                    this.healthIssues = data.data.issues || [];
                    this.healthLastCheck = data.data.last_check;

                    // If the modal is open, also refresh errors
                    if (this.showHealthModal) {
                        fetch('/api/health/errors?limit=20')
                            .then(r => r.ok ? r.json() : [])
                            .then(errors => { this.healthErrors = errors || []; })
                            .catch(() => {});
                    }
                }
            },

            /**
             * Called on WS reconnect — refresh health data if modal is open.
             * (app.js already sends get_health on connect, which triggers handleHealthUpdate.)
             */
            onHealthReconnect() {
                if (this.showHealthModal) {
                    this._fetchHealthData();
                }
            },

            /**
             * Clear all errors from the persistent error log.
             * Calls DELETE /api/health/errors and updates local state.
             */
            clearHealthErrors() {
                if (this.healthClearing) return;
                this.healthClearing = true;

                fetch('/api/health/errors', { method: 'DELETE' })
                    .then(r => {
                        if (!r.ok) throw new Error(`HTTP ${r.status}`);
                        return r.json();
                    })
                    .then(() => {
                        this.healthErrors = [];
                        this.healthClearing = false;
                        this.showToast('Error log cleared', 'success');
                    })
                    .catch(() => {
                        this.healthClearing = false;
                        this.showToast('Failed to clear error log', 'error');
                    });
            },
        };
    },
};

// Register with loader
if (window.PocketPaw.Loader) {
    window.PocketPaw.Loader.register('Health', window.PocketPaw.Health);
}

/**
 * PocketPaw - Analytics and Trace Explorer Feature Module
 *
 * Updated: 2026-04-21 — Added budget bar, alert bell, cost-by-tool table,
 *   channel uptime section and auto-refresh for alert count.
 *
 * Integrates dashboard UI with:
 * - /api/v1/analytics/*
 * - /api/v1/traces
 * - /api/v1/traces/{trace_id}
 * - /api/v1/budget/status
 * - /api/v1/alerts
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.Analytics = {
    name: 'Analytics',

    getState() {
        return {
            showAnalyticsModal: false,
            analyticsTab: 'overview', // overview | traces | budget | alerts
            analyticsPeriod: 'day',   // day | week | month
            analyticsLoading: false,
            analyticsError: '',

            analyticsCost: null,
            analyticsPerformance: null,
            analyticsUsage: null,
            analyticsHealth: null,

            // Budget state
            analyticsBudget: null,
            analyticsBudgetLoading: false,
            analyticsBudgetError: '',
            budgetOverrideCapInput: '',
            budgetOverrideReasonInput: '',
            budgetOverrideSaving: false,
            budgetOverrideError: '',
            budgetClearError: '',

            // Alerts state
            analyticsAlerts: [],
            analyticsAlertsLoading: false,
            analyticsAlertsError: '',
            alertUnreadCount: 0,
            _alertPollTimer: null,

            // Traces
            tracesLoading: false,
            tracesError: '',
            traces: [],
            traceSince: '',
            traceSessionFilter: '',
            traceMinCost: 0,
            traceLimit: 50,

            selectedTraceId: '',
            selectedTrace: null,
            selectedTraceLoading: false,
            selectedTraceError: '',

            _analyticsPollTimer: null,
        };
    },

    getMethods() {
        return {
            openAnalytics() {
                this.showAnalyticsModal = true;
                this.analyticsError = '';
                this.tracesError = '';
                this.selectedTraceError = '';
                this.refreshAnalyticsPanel();
                this._startAnalyticsPoll();
                this._startAlertPoll();
                this.$nextTick(() => {
                    if (window.refreshIcons) window.refreshIcons();
                });
            },

            closeAnalyticsPanel() {
                this.showAnalyticsModal = false;
                this._stopAnalyticsPoll();
                this._stopAlertPoll();
            },

            setAnalyticsTab(tab) {
                this.analyticsTab = tab;
                if (tab === 'traces' && (!Array.isArray(this.traces) || this.traces.length === 0)) {
                    this.refreshTraces();
                }
                if (tab === 'budget') {
                    this.refreshBudget();
                }
                if (tab === 'alerts') {
                    this.refreshAlerts();
                    this.markAlertsRead();
                }
            },

            setAnalyticsPeriod(period) {
                if (this.analyticsPeriod === period) return;
                this.analyticsPeriod = period;
                this.refreshAnalyticsData();
            },

            async refreshAnalyticsPanel() {
                await Promise.all([
                    this.refreshAnalyticsData(),
                    this.refreshTraces(),
                    this.refreshBudget(),
                    this.refreshAlertCount(),
                ]);
            },

            async _fetchAnalyticsJson(url) {
                const resp = await fetch(url);
                let data = null;
                try {
                    data = await resp.json();
                } catch (_) {
                    data = null;
                }

                if (!resp.ok) {
                    const detail = data && (data.detail || data.error);
                    throw new Error(detail || `Request failed (${resp.status})`);
                }
                return data;
            },

            async refreshAnalyticsData() {
                this.analyticsLoading = true;
                this.analyticsError = '';

                try {
                    const period = encodeURIComponent(this.analyticsPeriod || 'day');
                    // Single request — backend fetches traces once and passes the
                    // result to all four sub-functions (eliminates 4x parallel scans).
                    const all = await this._fetchAnalyticsJson(`/api/v1/analytics?period=${period}`);

                    this.analyticsCost = all.cost;
                    this.analyticsPerformance = all.performance;
                    this.analyticsUsage = all.usage;
                    this.analyticsHealth = all.health;
                } catch (err) {
                    this.analyticsError = err && err.message ? err.message : 'Failed to load analytics';
                } finally {
                    this.analyticsLoading = false;
                }
            },

            // ── Budget ─────────────────────────────────────────────────────────

            async refreshBudget() {
                this.analyticsBudgetLoading = true;
                this.analyticsBudgetError = '';
                try {
                    this.analyticsBudget = await this._fetchAnalyticsJson('/api/v1/budget/status');
                } catch (err) {
                    this.analyticsBudgetError = err && err.message ? err.message : 'Failed to load budget';
                } finally {
                    this.analyticsBudgetLoading = false;
                }
            },

            budgetPercent() {
                const b = this.analyticsBudget && this.analyticsBudget.budget;
                if (!b) return 0;
                const spent = Number(b.spent_usd || 0);
                const cap = Number(b.effective_cap_usd || this.analyticsBudget.configured_cap_usd || 0);
                if (!cap || cap <= 0) return 0;
                return Math.min(Math.round((spent / cap) * 1000) / 10, 100);
            },

            budgetBarClass() {
                const pct = this.budgetPercent();
                if (pct >= 100) return 'bg-danger';
                if (pct >= 80) return 'bg-warning';
                return 'bg-accent';
            },

            budgetSpent() {
                const b = this.analyticsBudget && this.analyticsBudget.budget;
                return b ? Number(b.spent_usd || 0) : 0;
            },

            budgetCap() {
                if (!this.analyticsBudget) return 0;
                const b = this.analyticsBudget.budget;
                const cfg = Number(this.analyticsBudget.configured_cap_usd || 0);
                if (!b) return cfg;
                return Number(b.effective_cap_usd || cfg || 0);
            },

            async setBudgetOverride() {
                const cap = Number(this.budgetOverrideCapInput);
                if (!cap || cap <= 0) {
                    this.budgetOverrideError = 'cap_usd must be > 0';
                    return;
                }
                this.budgetOverrideSaving = true;
                this.budgetOverrideError = '';
                try {
                    const resp = await fetch('/api/v1/budget/override', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            cap_usd: cap,
                            reason: this.budgetOverrideReasonInput.trim(),
                        }),
                    });
                    if (!resp.ok) {
                        let detail = `Request failed (${resp.status})`;
                        try {
                            const body = await resp.json();
                            if (body && body.detail) detail = String(body.detail);
                        } catch (_) {}
                        throw new Error(detail);
                    }
                    this.budgetOverrideCapInput = '';
                    this.budgetOverrideReasonInput = '';
                    await this.refreshBudget();
                } catch (err) {
                    this.budgetOverrideError = err && err.message ? err.message : 'Failed to set override';
                } finally {
                    this.budgetOverrideSaving = false;
                }
            },

            async clearBudgetOverride() {
                this.budgetClearError = '';
                try {
                    const resp = await fetch('/api/v1/budget/override', { method: 'DELETE' });
                    if (!resp.ok) {
                        let detail = `Request failed (${resp.status})`;
                        try {
                            const body = await resp.json();
                            if (body && body.detail) detail = String(body.detail);
                        } catch (_) {}
                        this.budgetClearError = detail;
                        return;
                    }
                    await this.refreshBudget();
                } catch (err) {
                    this.budgetClearError = err && err.message ? err.message : 'Failed to clear override';
                }
            },

            // ── Alerts ─────────────────────────────────────────────────────────

            async refreshAlerts() {
                this.analyticsAlertsLoading = true;
                this.analyticsAlertsError = '';
                try {
                    const data = await this._fetchAnalyticsJson('/api/v1/alerts?limit=100');
                    this.analyticsAlerts = Array.isArray(data.alerts) ? data.alerts : [];
                    this.alertUnreadCount = Number(data.unread_count || 0);
                } catch (err) {
                    this.analyticsAlertsError = err && err.message ? err.message : 'Failed to load alerts';
                } finally {
                    this.analyticsAlertsLoading = false;
                }
            },

            async refreshAlertCount() {
                try {
                    const data = await this._fetchAnalyticsJson('/api/v1/alerts?limit=1');
                    this.alertUnreadCount = Number(data.unread_count || 0);
                } catch (_) {}
            },

            async markAlertsRead() {
                try {
                    await fetch('/api/v1/alerts/mark-read', { method: 'POST' });
                    this.alertUnreadCount = 0;
                } catch (_) {}
            },

            alertSeverityClass(severity) {
                const s = String(severity || '').toLowerCase();
                if (s === 'critical') return 'bg-danger/20 text-danger border border-danger/30';
                if (s === 'warning') return 'bg-warning/20 text-warning border border-warning/30';
                return 'bg-white/10 text-white/60 border border-white/15';
            },

            _startAlertPoll() {
                this._stopAlertPoll();
                this._alertPollTimer = setInterval(() => {
                    if (!this.showAnalyticsModal) {
                        this._stopAlertPoll();
                        return;
                    }
                    this.refreshAlertCount();
                }, 30000);
            },

            _stopAlertPoll() {
                if (this._alertPollTimer) {
                    clearInterval(this._alertPollTimer);
                    this._alertPollTimer = null;
                }
            },

            // ── Traces ─────────────────────────────────────────────────────────

            async refreshTraces() {
                this.tracesLoading = true;
                this.tracesError = '';

                try {
                    const params = new URLSearchParams();
                    if (this.traceSince && this.traceSince.trim()) {
                        params.set('since', this.traceSince.trim());
                    }
                    if (this.traceSessionFilter && this.traceSessionFilter.trim()) {
                        params.set('session_id', this.traceSessionFilter.trim());
                    }
                    params.set('min_cost', String(Math.max(0, Number(this.traceMinCost) || 0)));
                    params.set('limit', String(Math.max(1, Number(this.traceLimit) || 50)));

                    const traces = await this._fetchAnalyticsJson(`/api/v1/traces?${params.toString()}`);
                    this.traces = Array.isArray(traces) ? traces : [];

                    if (!this.selectedTraceId && this.traces.length > 0) {
                        await this.selectTrace(this.traces[0].trace_id);
                    } else if (this.selectedTraceId) {
                        const found = this.traces.some(t => t.trace_id === this.selectedTraceId);
                        if (found) {
                            await this.selectTrace(this.selectedTraceId);
                        } else {
                            this.selectedTraceId = '';
                            this.selectedTrace = null;
                        }
                    }
                } catch (err) {
                    this.tracesError = err && err.message ? err.message : 'Failed to load traces';
                } finally {
                    this.tracesLoading = false;
                }
            },

            clearTraceFilters() {
                this.traceSince = '';
                this.traceSessionFilter = '';
                this.traceMinCost = 0;
                this.traceLimit = 50;
                this.refreshTraces();
            },

            async selectTrace(traceId) {
                if (!traceId) return;

                this.selectedTraceId = traceId;
                this.selectedTraceLoading = true;
                this.selectedTraceError = '';

                try {
                    this.selectedTrace = await this._fetchAnalyticsJson(
                        `/api/v1/traces/${encodeURIComponent(traceId)}`
                    );
                } catch (err) {
                    this.selectedTrace = null;
                    this.selectedTraceError =
                        err && err.message ? err.message : 'Failed to load trace detail';
                } finally {
                    this.selectedTraceLoading = false;
                }
            },

            _startAnalyticsPoll() {
                this._stopAnalyticsPoll();
                this._analyticsPollTimer = setInterval(() => {
                    if (!this.showAnalyticsModal) {
                        this._stopAnalyticsPoll();
                        return;
                    }

                    this.refreshAnalyticsData();
                    if (this.analyticsTab === 'traces') {
                        this.refreshTraces();
                    }
                    if (this.analyticsTab === 'budget') {
                        this.refreshBudget();
                    }
                    this.refreshAlertCount();
                }, 30000);
            },

            _stopAnalyticsPoll() {
                if (this._analyticsPollTimer) {
                    clearInterval(this._analyticsPollTimer);
                    this._analyticsPollTimer = null;
                }
            },

            // ── View helpers ───────────────────────────────────────────────────

            analyticsCostTotals() {
                return (this.analyticsCost && this.analyticsCost.totals) || {};
            },

            analyticsLatencyStats() {
                return (this.analyticsPerformance && this.analyticsPerformance.response_latency_ms) || {};
            },

            analyticsUsageTotals() {
                return (this.analyticsUsage && this.analyticsUsage.totals) || {};
            },

            analyticsTopModels(limit = 6) {
                const rows = this.analyticsCost && Array.isArray(this.analyticsCost.by_model)
                    ? this.analyticsCost.by_model
                    : [];
                return rows.slice(0, limit);
            },

            analyticsTopTools(limit = 8) {
                const rows = this.analyticsPerformance && Array.isArray(this.analyticsPerformance.tool_performance)
                    ? this.analyticsPerformance.tool_performance
                    : [];
                return rows.slice(0, limit);
            },

            analyticsTopToolsByCost(limit = 8) {
                const rows = this.analyticsCost && Array.isArray(this.analyticsCost.by_tool)
                    ? this.analyticsCost.by_tool
                    : [];
                return rows.slice(0, limit);
            },

            analyticsTopChannels(limit = 6) {
                const rows = this.analyticsUsage && Array.isArray(this.analyticsUsage.messages_by_channel)
                    ? this.analyticsUsage.messages_by_channel
                    : [];
                return rows.slice(0, limit);
            },

            analyticsChannelUptime() {
                const health = this.analyticsHealth;
                if (!health || !health.channel_health) return [];
                const uptime = health.channel_health.uptime;
                return Array.isArray(uptime) ? uptime : Object.values(uptime || {});
            },

            analyticsFmtCurrency(value) {
                const n = Number(value);
                if (!Number.isFinite(n)) return '$0.00';

                if (Math.abs(n) >= 1) {
                    return `$${n.toLocaleString(undefined, {
                        minimumFractionDigits: 2,
                        maximumFractionDigits: 2,
                    })}`;
                }

                return `$${n.toLocaleString(undefined, {
                    minimumFractionDigits: 4,
                    maximumFractionDigits: 4,
                })}`;
            },

            analyticsFmtNumber(value) {
                const n = Number(value);
                if (!Number.isFinite(n)) return '0';
                return n.toLocaleString();
            },

            analyticsFmtPercent(value, assumeFraction = true) {
                const n = Number(value);
                if (!Number.isFinite(n)) return '0%';
                const normalized = assumeFraction ? n * 100 : n;
                return `${normalized.toFixed(2)}%`;
            },

            analyticsFmtMs(value) {
                const n = Number(value);
                if (!Number.isFinite(n)) return '0 ms';
                return `${n.toFixed(1)} ms`;
            },

            analyticsFmtBytes(value) {
                const n = Number(value);
                if (!Number.isFinite(n) || n <= 0) return '0 B';

                const units = ['B', 'KB', 'MB', 'GB', 'TB'];
                let size = n;
                let unitIndex = 0;
                while (size >= 1024 && unitIndex < units.length - 1) {
                    size /= 1024;
                    unitIndex += 1;
                }
                return `${size.toFixed(unitIndex === 0 ? 0 : 2)} ${units[unitIndex]}`;
            },

            analyticsFmtWhen(value) {
                if (!value) return '-';
                const d = new Date(value);
                if (Number.isNaN(d.getTime())) return String(value);
                return d.toLocaleString();
            },

            analyticsShort(value, maxLength = 42) {
                const text = String(value || '');
                if (!text) return '';
                if (text.length <= maxLength) return text;
                return `${text.slice(0, maxLength - 3)}...`;
            },

            analyticsStatusClass(status) {
                const normalized = String(status || '').toLowerCase();
                if (normalized === 'ok' || normalized === 'healthy') {
                    return 'bg-success/20 text-success border border-success/30';
                }
                if (normalized === 'warning' || normalized === 'degraded') {
                    return 'bg-warning/20 text-warning border border-warning/30';
                }
                if (normalized === 'error' || normalized === 'unhealthy' || normalized === 'critical') {
                    return 'bg-danger/20 text-danger border border-danger/30';
                }
                if (normalized === 'command') {
                    return 'bg-accent/20 text-accent border border-accent/30';
                }
                return 'bg-white/10 text-white/60 border border-white/15';
            },

            analyticsTraceErrorSummary(trace) {
                if (!trace || !Array.isArray(trace.errors) || trace.errors.length === 0) return '';
                const first = trace.errors[0] || {};
                const message = first.message || 'Trace has errors';
                return this.analyticsShort(message, 120);
            },
        };
    },
};

if (window.PocketPaw.Loader) {
    window.PocketPaw.Loader.register('Analytics', window.PocketPaw.Analytics);
}

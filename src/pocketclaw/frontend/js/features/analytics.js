/**
 * PocketPaw - Analytics Feature Module
 *
 * Created: 2026-02-13
 *
 * Agent usage analytics: tool frequency, response times, error rates,
 * session statistics.  Fetches from /api/analytics/* REST endpoints.
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.Analytics = {
    name: 'Analytics',
    getState() {
        return {
            analyticsData: null,
            analyticsTools: [],
            analyticsTimeline: [],
            analyticsLoading: false,
            analyticsError: null,
            analyticsAutoRefresh: null,
        };
    },

    getMethods() {
        return {
            /**
             * Load all analytics data from the API
             */
            async loadAnalytics() {
                this.analyticsLoading = true;
                this.analyticsError = null;
                try {
                    const [summaryRes, toolsRes, timelineRes] = await Promise.all([
                        fetch('/api/analytics/summary'),
                        fetch('/api/analytics/tools'),
                        fetch('/api/analytics/timeline'),
                    ]);

                    if (summaryRes.ok) {
                        this.analyticsData = await summaryRes.json();
                    }
                    if (toolsRes.ok) {
                        const td = await toolsRes.json();
                        this.analyticsTools = td.tools || [];
                    }
                    if (timelineRes.ok) {
                        const tl = await timelineRes.json();
                        this.analyticsTimeline = tl.timeline || [];
                    }
                } catch (e) {
                    console.error('[Analytics] Failed to load:', e);
                    this.analyticsError = e.message || 'Failed to load analytics';
                } finally {
                    this.analyticsLoading = false;
                }
            },

            /**
             * Start auto-refresh when analytics view is active
             */
            startAnalyticsRefresh() {
                this.loadAnalytics();
                if (this.analyticsAutoRefresh) clearInterval(this.analyticsAutoRefresh);
                this.analyticsAutoRefresh = setInterval(() => {
                    if (this.view === 'analytics') {
                        this.loadAnalytics();
                    }
                }, 30000); // Refresh every 30 seconds
            },

            /**
             * Stop auto-refresh
             */
            stopAnalyticsRefresh() {
                if (this.analyticsAutoRefresh) {
                    clearInterval(this.analyticsAutoRefresh);
                    this.analyticsAutoRefresh = null;
                }
            },

            /**
             * Format milliseconds into human-readable duration
             */
            formatDuration(ms) {
                if (ms == null || ms === 0) return '—';
                if (ms < 1000) return `${Math.round(ms)}ms`;
                if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
                return `${(ms / 60000).toFixed(1)}m`;
            },

            /**
             * Format uptime seconds into human-readable string
             */
            formatUptime(seconds) {
                if (!seconds) return '—';
                const h = Math.floor(seconds / 3600);
                const m = Math.floor((seconds % 3600) / 60);
                if (h > 0) return `${h}h ${m}m`;
                return `${m}m`;
            },

            /**
             * Format error rate as percentage
             */
            formatErrorRate(rate) {
                if (rate == null) return '0%';
                return `${(rate * 100).toFixed(1)}%`;
            },

            /**
             * Get bar width for tool chart (relative to max)
             */
            getToolBarWidth(tool) {
                if (!this.analyticsTools.length) return '0%';
                const max = this.analyticsTools[0].call_count || 1;
                return `${Math.max(2, (tool.call_count / max) * 100)}%`;
            },

            /**
             * Build SVG sparkline path from timeline data
             */
            getSparklinePath(field) {
                const data = this.analyticsTimeline;
                if (!data || data.length < 2) return '';

                const values = data.map(d => d[field] || 0);
                const max = Math.max(...values, 1);
                const width = 280;
                const height = 60;
                const step = width / (values.length - 1);

                let path = '';
                values.forEach((v, i) => {
                    const x = i * step;
                    const y = height - (v / max) * (height - 4);
                    path += (i === 0 ? 'M' : 'L') + ` ${x.toFixed(1)} ${y.toFixed(1)}`;
                });
                return path;
            },

            /**
             * Get max value for sparkline Y-axis label
             */
            getSparklineMax(field) {
                const data = this.analyticsTimeline;
                if (!data || !data.length) return 0;
                return Math.max(...data.map(d => d[field] || 0));
            }
        };
    }
};

window.PocketPaw.Loader.register('Analytics', window.PocketPaw.Analytics);

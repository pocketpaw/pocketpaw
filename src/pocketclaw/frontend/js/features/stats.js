/**
 * PocketPaw - Agent Stats Feature
 * Handles agent performance statistics tracking and display
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.Stats = {
    getState() {
        return {
            stats: {
                summary: {
                    total_calls: 0,
                    successful_calls: 0,
                    failed_calls: 0,
                    success_rate: 0,
                    avg_response_time_ms: 0,
                    total_tokens: 0,
                    total_input_tokens: 0,
                    total_output_tokens: 0,
                    avg_tokens_per_call: 0
                },
                recent: []
            }
        };
    },

    getMethods() {
        return {
            /**
             * Fetch stats from the API
             */
            async refreshStats() {
                try {
                    // Fetch summary
                    const summaryResponse = await fetch('/api/stats/summary');
                    if (summaryResponse.ok) {
                        this.stats.summary = await summaryResponse.json();
                    }

                    // Fetch recent calls
                    const recentResponse = await fetch('/api/stats/recent?limit=20');
                    if (recentResponse.ok) {
                        const data = await recentResponse.json();
                        this.stats.recent = data.calls || [];
                    }

                    this.log('Stats refreshed', 'info');
                } catch (error) {
                    console.error('Failed to fetch stats:', error);
                    this.log('Failed to fetch stats', 'error');
                }
            },

            /**
             * Handle real-time stats updates from WebSocket
             */
            handleStatsUpdate(data) {
                // Update summary by refetching (simple approach)
                this.refreshStats();

                // Log the new stat
                const time = data.response_time_ms?.toFixed(0) || '?';
                const tokens = data.total_tokens || 0;
                const status = data.success ? '✓' : '✗';

                this.log(
                    `Agent call: ${status} ${time}ms, ${tokens} tokens`,
                    data.success ? 'success' : 'error'
                );
            },

            /**
             * Clear all statistics
             */
            async clearStats() {
                if (!confirm('Are you sure you want to clear all statistics?')) {
                    return;
                }

                try {
                    const response = await fetch('/api/stats/clear', {
                        method: 'POST'
                    });

                    if (response.ok) {
                        await this.refreshStats();
                        this.showToast('Statistics cleared', 'success');
                        this.log('Statistics cleared', 'info');
                    } else {
                        throw new Error('Failed to clear stats');
                    }
                } catch (error) {
                    console.error('Failed to clear stats:', error);
                    this.showToast('Failed to clear statistics', 'error');
                }
            }
        };
    }
};

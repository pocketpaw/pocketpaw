/**
 * PocketPaw - Remote Access Feature Module
 *
 * Created: 2026-02-05
 * Extracted from app.js as part of componentization refactor.
 *
 * Contains remote access features:
 * - Cloudflare Tunnel management
 * - Telegram integration
 * - Token management
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.RemoteAccess = {
    name: 'RemoteAccess',
    /**
     * Get initial state for Remote Access
     */
    getState() {
        return {
            showRemote: false,
            remoteTab: 'qr',  // 'qr' or 'telegram'
            remoteStatus: { active: false, url: '', installed: false },
            tunnelLoading: false,

            // Telegram state
            telegramStatus: { configured: false, user_id: null },
            telegramForm: { botToken: '', qrCode: '', error: '' },
            telegramLoading: false,
            telegramPollInterval: null
        };
    },

    /**
     * Get methods for Remote Access
     */
    getMethods() {
        return {
            // ==================== Cloudflare Tunnel ====================

            /**
             * Open Remote Access modal
             */
            async openRemote() {
                this.showRemote = true;
                this.tunnelLoading = true;

                try {
                    const res = await fetch('/api/remote/status');
                    if (res.ok) {
                        this.remoteStatus = await res.json();
                    }
                } catch (e) {
                    console.error('Failed to get tunnel status', e);
                } finally {
                    this.tunnelLoading = false;
                }
            },

            /**
             * Toggle Cloudflare Tunnel
             */
            async toggleTunnel() {
                this.tunnelLoading = true;
                try {
                    const endpoint = this.remoteStatus.active ? '/api/remote/stop' : '/api/remote/start';
                    const res = await fetch(endpoint, { method: 'POST' });
                    const data = await res.json();

                    if (data.error) {
                        this.showToast(data.error, 'error');
                    } else {
                        // Refresh status
                        const statusRes = await fetch('/api/remote/status');
                        this.remoteStatus = await statusRes.json();

                        if (this.remoteStatus.active) {
                            this.showToast('Tunnel Started! You can now access remotely.', 'success');
                        } else {
                            this.showToast('Tunnel Stopped.', 'info');
                        }
                    }
                } catch (e) {
                    this.showToast('Failed to toggle tunnel: ' + e.message, 'error');
                } finally {
                    this.tunnelLoading = false;
                }
            },

            /**
             * Regenerate Access Token
             */
            async regenerateToken() {
                if (!confirm('Are you sure? This will invalidate all existing sessions (including your phone).')) return;

                try {
                    const res = await fetch('/api/token/regenerate', { method: 'POST' });
                    const data = await res.json();

                    if (data.token) {
                        localStorage.setItem('pocketpaw_token', data.token);
                        this.showToast('Token regenerated! Please re-scan the QR code.', 'success');
                        // Force refresh QR code image
                        this.showRemote = false;
                        setTimeout(() => { this.showRemote = true; }, 100);
                    }
                } catch (e) {
                    this.showToast('Failed to regenerate token', 'error');
                }
            },

            // ==================== Telegram ====================

            /**
             * Get Telegram configuration status
             */
            async getTelegramStatus() {
                try {
                    const res = await fetch('/api/telegram/status');
                    if (res.ok) {
                        this.telegramStatus = await res.json();
                    }
                } catch (e) {
                    console.error('Failed to get Telegram status', e);
                }
            },

            /**
             * Start Telegram pairing flow
             */
            async startTelegramPairing() {
                this.telegramLoading = true;
                this.telegramForm.error = '';
                this.telegramForm.qrCode = '';

                try {
                    const res = await fetch('/api/telegram/setup', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ bot_token: this.telegramForm.botToken })
                    });
                    const data = await res.json();

                    if (data.error) {
                        this.telegramForm.error = data.error;
                    } else if (data.qr_url) {
                        this.telegramForm.qrCode = data.qr_url;
                        // Start polling for pairing completion
                        this.startTelegramPolling();
                    }
                } catch (e) {
                    this.telegramForm.error = 'Failed to connect: ' + e.message;
                } finally {
                    this.telegramLoading = false;
                }
            },

            /**
             * Poll for Telegram pairing completion
             */
            startTelegramPolling() {
                // Clear any existing interval
                if (this.telegramPollInterval) {
                    clearInterval(this.telegramPollInterval);
                }

                this.telegramPollInterval = setInterval(async () => {
                    try {
                        const res = await fetch('/api/telegram/pairing-status');
                        const data = await res.json();

                        if (data.paired) {
                            clearInterval(this.telegramPollInterval);
                            this.telegramPollInterval = null;
                            this.telegramForm.qrCode = '';
                            this.telegramForm.botToken = '';
                            this.telegramStatus = { configured: true, user_id: data.user_id };
                            this.showToast('Telegram connected successfully!', 'success');
                            // Reinit icons for the success state
                            setTimeout(() => lucide.createIcons(), 100);
                        }
                    } catch (e) {
                        console.error('Polling error', e);
                    }
                }, 2000);
            },

            /**
             * Stop Telegram polling (cleanup)
             */
            stopTelegramPolling() {
                if (this.telegramPollInterval) {
                    clearInterval(this.telegramPollInterval);
                    this.telegramPollInterval = null;
                }
            }
        };
    }
};

window.PocketPaw.Loader.register('RemoteAccess', window.PocketPaw.RemoteAccess);

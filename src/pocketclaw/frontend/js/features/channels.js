/**
 * PocketPaw - Channels Feature Module
 *
 * Created: 2026-02-06
 *
 * Contains channel management state and methods:
 * - Channel status polling
 * - Save channel configuration (tokens)
 * - Start/Stop channel adapters dynamically
 * - WhatsApp personal mode QR polling
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.Channels = {
    name: 'Channels',
    /**
     * Get initial state for Channels
     */
    getState() {
        return {
            showChannels: false,
            channelsTab: 'discord',
            channelsMobileView: 'list',
            channelStatus: {
                discord: { configured: false, running: false },
                slack: { configured: false, running: false },
                whatsapp: { configured: false, running: false, mode: 'personal' },
                telegram: { configured: false, running: false },
                signal: { configured: false, running: false },
                matrix: { configured: false, running: false },
                teams: { configured: false, running: false },
                google_chat: { configured: false, running: false }
            },
            channelForms: {
                discord: { bot_token: '' },
                slack: { bot_token: '', app_token: '' },
                whatsapp: { access_token: '', phone_number_id: '', verify_token: '' },
                telegram: { bot_token: '' },
                signal: { api_url: '', phone_number: '' },
                matrix: { homeserver: '', user_id: '', access_token: '' },
                teams: { app_id: '', app_password: '' },
                google_chat: { service_account_key: '', project_id: '', subscription_id: '', _mode: 'webhook' }
            },
            channelLoading: false,
            // WhatsApp personal mode QR state
            whatsappQr: null,
            whatsappConnected: false,
            whatsappQrPolling: null,
            // Generic webhooks
            webhookSlots: [],
            showAddWebhook: false,
            newWebhookName: '',
            newWebhookDescription: ''
        };
    },

    /**
     * Get methods for Channels
     */
    getMethods() {
        return {
            /**
             * Display name for channel tabs
             */
            channelDisplayName(tab) {
                const names = {
                    discord: 'Discord',
                    slack: 'Slack',
                    whatsapp: 'WhatsApp',
                    telegram: 'Telegram',
                    signal: 'Signal',
                    matrix: 'Matrix',
                    teams: 'Teams',
                    google_chat: 'GChat',
                    webhooks: 'Webhooks'
                };
                return names[tab] || tab;
            },

            /**
             * Lucide icon name for each channel
             */
            channelIcon(tab) {
                const icons = {
                    discord: 'gamepad-2',
                    slack: 'hash',
                    whatsapp: 'phone',
                    telegram: 'send',
                    signal: 'shield',
                    matrix: 'grid-3x3',
                    teams: 'users',
                    google_chat: 'message-circle',
                    webhooks: 'webhook'
                };
                return icons[tab] || 'circle';
            },

            /**
             * Setup guide URL per channel
             */
            channelGuideUrl(tab) {
                const urls = {
                    discord: 'https://discord.com/developers/applications',
                    slack: 'https://api.slack.com/apps',
                    whatsapp: 'https://developers.facebook.com/apps/',
                    telegram: 'https://t.me/BotFather',
                    signal: 'https://github.com/bbernhard/signal-cli-rest-api',
                    matrix: 'https://matrix.org/docs/guides/',
                    teams: 'https://dev.botframework.com/',
                    google_chat: 'https://developers.google.com/workspace/chat'
                };
                return urls[tab] || null;
            },

            /**
             * Setup guide link label per channel
             */
            channelGuideLabel(tab) {
                const labels = {
                    discord: 'Discord Dev Portal',
                    slack: 'Slack App Dashboard',
                    whatsapp: 'Meta Dev Portal',
                    telegram: '@BotFather',
                    signal: 'signal-cli-rest-api',
                    matrix: 'Matrix.org Docs',
                    teams: 'Bot Framework',
                    google_chat: 'Google Chat API'
                };
                return labels[tab] || 'Setup Guide';
            },

            /**
             * Open Channels modal and fetch status
             */
            async openChannels() {
                this.showChannels = true;
                await this.getChannelStatus();
                await this.loadWebhooks();
                this.startWhatsAppQrPollingIfNeeded();
                this.$nextTick(() => {
                    if (window.refreshIcons) window.refreshIcons();
                });
            },

            /**
             * Fetch channel status from backend
             */
            async getChannelStatus() {
                try {
                    const res = await fetch('/api/channels/status');
                    if (res.ok) {
                        this.channelStatus = await res.json();
                    }
                } catch (e) {
                    console.error('Failed to get channel status', e);
                }
            },

            /**
             * Save channel config (tokens) to backend
             */
            async saveChannelConfig(channel) {
                this.channelLoading = true;
                try {
                    const res = await fetch('/api/channels/save', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            channel,
                            config: this.channelForms[channel]
                        })
                    });
                    const data = await res.json();
                    if (data.status === 'ok') {
                        this.showToast(`${channel.charAt(0).toUpperCase() + channel.slice(1)} config saved!`, 'success');
                        // Clear form inputs after save
                        for (const key in this.channelForms[channel]) {
                            this.channelForms[channel][key] = '';
                        }
                        await this.getChannelStatus();
                    } else {
                        this.showToast(data.error || 'Failed to save', 'error');
                    }
                } catch (e) {
                    this.showToast('Failed to save config: ' + e.message, 'error');
                } finally {
                    this.channelLoading = false;
                }
            },

            /**
             * Save WhatsApp mode (personal/business)
             */
            async saveWhatsAppMode(mode) {
                this.channelLoading = true;
                try {
                    // Stop adapter if running (mode change requires restart)
                    if (this.channelStatus.whatsapp?.running) {
                        await fetch('/api/channels/toggle', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ channel: 'whatsapp', action: 'stop' })
                        });
                    }

                    const res = await fetch('/api/channels/save', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            channel: 'whatsapp',
                            config: { mode }
                        })
                    });
                    const data = await res.json();
                    if (data.status === 'ok') {
                        this.showToast(`WhatsApp mode set to ${mode}`, 'success');
                        await this.getChannelStatus();
                        this.whatsappQr = null;
                        this.whatsappConnected = false;
                        this.startWhatsAppQrPollingIfNeeded();
                    }
                } catch (e) {
                    this.showToast('Failed to save mode: ' + e.message, 'error');
                } finally {
                    this.channelLoading = false;
                    this.$nextTick(() => {
                        if (window.refreshIcons) window.refreshIcons();
                    });
                }
            },

            /**
             * Toggle (start/stop) a channel adapter
             */
            async toggleChannel(channel) {
                this.channelLoading = true;
                const isRunning = this.channelStatus[channel]?.running;
                const action = isRunning ? 'stop' : 'start';

                try {
                    const res = await fetch('/api/channels/toggle', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ channel, action })
                    });
                    const data = await res.json();

                    if (data.error) {
                        this.showToast(data.error, 'error');
                    } else {
                        const label = channel.charAt(0).toUpperCase() + channel.slice(1);
                        this.showToast(
                            action === 'start' ? `${label} started!` : `${label} stopped.`,
                            action === 'start' ? 'success' : 'info'
                        );
                        await this.getChannelStatus();

                        // Start/stop QR polling for WhatsApp personal mode
                        if (channel === 'whatsapp') {
                            if (action === 'start') {
                                this.startWhatsAppQrPollingIfNeeded();
                            } else {
                                this.stopWhatsAppQrPolling();
                                this.whatsappQr = null;
                                this.whatsappConnected = false;
                            }
                        }
                    }
                } catch (e) {
                    this.showToast('Failed to toggle channel: ' + e.message, 'error');
                } finally {
                    this.channelLoading = false;
                    this.$nextTick(() => {
                        if (window.refreshIcons) window.refreshIcons();
                    });
                }
            },

            /**
             * Start QR polling if WhatsApp is running in personal mode
             */
            startWhatsAppQrPollingIfNeeded() {
                this.stopWhatsAppQrPolling();
                const isPersonal = this.channelStatus.whatsapp?.mode === 'personal';
                const isRunning = this.channelStatus.whatsapp?.running;
                if (isPersonal && isRunning && !this.whatsappConnected) {
                    this.pollWhatsAppQr();
                    this.whatsappQrPolling = setInterval(() => this.pollWhatsAppQr(), 2000);
                }
            },

            /**
             * Poll the WhatsApp QR endpoint
             */
            async pollWhatsAppQr() {
                try {
                    const res = await fetch('/api/whatsapp/qr');
                    if (res.ok) {
                        const data = await res.json();
                        this.whatsappQr = data.qr;
                        this.whatsappConnected = data.connected;
                        if (data.connected) {
                            this.stopWhatsAppQrPolling();
                            await this.getChannelStatus();
                            this.$nextTick(() => {
                                if (window.refreshIcons) window.refreshIcons();
                            });
                        }
                    }
                } catch (e) {
                    console.error('Failed to poll WhatsApp QR', e);
                }
            },

            /**
             * Stop QR polling
             */
            stopWhatsAppQrPolling() {
                if (this.whatsappQrPolling) {
                    clearInterval(this.whatsappQrPolling);
                    this.whatsappQrPolling = null;
                }
            },

            /**
             * Get the count of running channels (for sidebar badge)
             */
            runningChannelCount() {
                return Object.values(this.channelStatus).filter(s => s.running).length;
            },

            /**
             * Load webhook slots from backend
             */
            async loadWebhooks() {
                try {
                    const res = await fetch('/api/webhooks');
                    if (res.ok) {
                        const data = await res.json();
                        this.webhookSlots = data.webhooks || [];
                    }
                } catch (e) {
                    console.error('Failed to load webhooks', e);
                }
            },

            /**
             * Add a new webhook slot
             */
            async addWebhook() {
                if (!this.newWebhookName.trim()) return;
                this.channelLoading = true;
                try {
                    const res = await fetch('/api/webhooks/add', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            name: this.newWebhookName.trim(),
                            description: this.newWebhookDescription.trim()
                        })
                    });
                    const data = await res.json();
                    if (data.status === 'ok') {
                        this.showToast('Webhook created!', 'success');
                        this.newWebhookName = '';
                        this.newWebhookDescription = '';
                        this.showAddWebhook = false;
                        await this.loadWebhooks();
                    } else {
                        this.showToast(data.detail || 'Failed to create webhook', 'error');
                    }
                } catch (e) {
                    this.showToast('Failed to create webhook: ' + e.message, 'error');
                } finally {
                    this.channelLoading = false;
                }
            },

            /**
             * Remove a webhook slot
             */
            async removeWebhook(name) {
                if (!confirm(`Remove webhook "${name}"?`)) return;
                try {
                    const res = await fetch('/api/webhooks/remove', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ name })
                    });
                    const data = await res.json();
                    if (data.status === 'ok') {
                        this.showToast('Webhook removed', 'info');
                        await this.loadWebhooks();
                    } else {
                        this.showToast(data.detail || 'Failed to remove', 'error');
                    }
                } catch (e) {
                    this.showToast('Failed to remove webhook: ' + e.message, 'error');
                }
            },

            /**
             * Regenerate a webhook slot's secret
             */
            async regenerateWebhookSecret(name) {
                if (!confirm(`Regenerate secret for "${name}"? Existing integrations will break.`)) return;
                try {
                    const res = await fetch('/api/webhooks/regenerate-secret', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ name })
                    });
                    const data = await res.json();
                    if (data.status === 'ok') {
                        this.showToast('Secret regenerated', 'success');
                        await this.loadWebhooks();
                    } else {
                        this.showToast(data.detail || 'Failed to regenerate', 'error');
                    }
                } catch (e) {
                    this.showToast('Failed to regenerate: ' + e.message, 'error');
                }
            },

            /**
             * Generate a QR code as a data URL (client-side, no external API)
             */
            generateQrDataUrl(data) {
                if (!data || typeof qrcode === 'undefined') return '';
                try {
                    const qr = qrcode(0, 'L');
                    qr.addData(data);
                    qr.make();
                    return qr.createDataURL(4, 0);
                } catch (e) {
                    console.error('QR generation failed', e);
                    return '';
                }
            },

            /**
             * Copy text to clipboard
             */
            async copyToClipboard(text) {
                try {
                    await navigator.clipboard.writeText(text);
                    this.showToast('Copied!', 'success');
                } catch (e) {
                    this.showToast('Failed to copy', 'error');
                }
            }
        };
    }
};

window.PocketPaw.Loader.register('Channels', window.PocketPaw.Channels);

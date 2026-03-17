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
                discord: { configured: false, running: false, autostart: true },
                slack: { configured: false, running: false, autostart: true },
                whatsapp: { configured: false, running: false, mode: '', autostart: true },
                telegram: { configured: false, running: false, autostart: true },
                signal: { configured: false, running: false, autostart: true },
                matrix: { configured: false, running: false, autostart: true },
                teams: { configured: false, running: false, autostart: true },
                google_chat: { configured: false, running: false, autostart: true }
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
            // Auto-install prompt state
            installPrompt: null,   // { channel, package, pipSpec } or null
            installLoading: false,
            // Discord settings (separate from token form)
            discordSettings: {
                bot_name: 'Paw',
                status_type: 'online',
                activity_type: '',
                activity_text: '',
                allowed_guild_ids: '',
                allowed_user_ids: '',
                allowed_channel_ids: '',
                conversation_channel_ids: ''
            },
            // Generic webhooks
            webhookSlots: [],
            showAddWebhook: false,
            newWebhookName: '',
            newWebhookDescription: '',
            // Alpine.js confirm dialog (replaces native confirm())
            confirmDialog: null, // { title, message, onConfirm, onCancel } or null
        };
    },

    /**
     * Get methods for Channels
     */
    getMethods() {
        return {
            /**
             * Show an Alpine.js confirmation dialog instead of native confirm()
             */
            showConfirm(title, message, onConfirm, onCancel = null) {
                this.confirmDialog = { title, message, onConfirm, onCancel };
                this.$nextTick(() => { if (window.refreshIcons) window.refreshIcons(); });
            },

            /**
             * Execute the confirm action and close the dialog
             */
            async confirmDialogAction() {
                const cb = this.confirmDialog?.onConfirm;
                this.confirmDialog = null;
                if (cb) await cb();
            },

            /**
             * Dismiss the confirm dialog (cancel)
             */
            async dismissConfirmDialog() {
                const cb = this.confirmDialog?.onCancel;
                this.confirmDialog = null;
                if (cb) await cb();
            },

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
             * Toggle auto-start on launch for a channel
             */
            async toggleAutostart(channel) {
                const current = this.channelStatus[channel]?.autostart !== false;
                const newVal = !current;
                try {
                    const res = await fetch('/api/channels/save', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ channel, config: { autostart: newVal } })
                    });
                    const data = await res.json();
                    if (data.status === 'ok') {
                        this.channelStatus[channel].autostart = newVal;
                        this.showToast(
                            `${this.channelDisplayName(channel)} auto-start ${newVal ? 'enabled' : 'disabled'}`,
                            newVal ? 'success' : 'info'
                        );
                    }
                } catch (e) {
                    this.showToast('Failed to update auto-start: ' + e.message, 'error');
                }
            },

            /**
             * Open Channels modal and fetch status
             */
            async openChannels() {
                this.showChannels = true;
                await this.getChannelStatus();
                await this.loadWebhooks();
                this.startWhatsAppQrPollingIfNeeded();

                // Check if there's a pending channel to start after restart
                const pendingChannel = sessionStorage.getItem('pendingChannelStart');
                if (pendingChannel) {
                    sessionStorage.removeItem('pendingChannelStart');
                    this.showToast(`Reconnected! Click Start to activate ${pendingChannel}.`, 'success');
                }

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
                        this.loadDiscordSettings();
                    }
                } catch (e) {
                    console.error('Failed to get channel status', e);
                }
            },

            /**
             * Load Discord settings from the status response into the form
             */
            loadDiscordSettings() {
                const d = this.channelStatus.discord;
                if (!d) return;
                this.discordSettings.bot_name = d.bot_name || 'Paw';
                this.discordSettings.status_type = d.status_type || 'online';
                this.discordSettings.activity_type = d.activity_type || '';
                this.discordSettings.activity_text = d.activity_text || '';
                this.discordSettings.allowed_guild_ids = (d.allowed_guild_ids || []).join(', ');
                this.discordSettings.allowed_user_ids = (d.allowed_user_ids || []).join(', ');
                this.discordSettings.allowed_channel_ids = (d.allowed_channel_ids || []).join(', ');
                this.discordSettings.conversation_channel_ids = (d.conversation_channel_ids || []).join(', ');
            },

            /**
             * Parse a comma-separated string of IDs into an array of integers
             */
            _parseIds(str) {
                if (!str || !str.trim()) return [];
                return str.split(',')
                    .map(s => s.trim())
                    .filter(s => s && /^\d+$/.test(s))
                    .map(Number);
            },

            /**
             * Save Discord settings (non-token fields)
             */
            async saveDiscordSettings() {
                this.channelLoading = true;
                try {
                    const config = {
                        bot_name: this.discordSettings.bot_name.trim() || 'Paw',
                        status_type: this.discordSettings.status_type,
                        activity_type: this.discordSettings.activity_type,
                        activity_text: this.discordSettings.activity_text,
                        allowed_guild_ids: this._parseIds(this.discordSettings.allowed_guild_ids),
                        allowed_user_ids: this._parseIds(this.discordSettings.allowed_user_ids),
                        allowed_channel_ids: this._parseIds(this.discordSettings.allowed_channel_ids),
                        conversation_channel_ids: this._parseIds(this.discordSettings.conversation_channel_ids)
                    };
                    const res = await fetch('/api/channels/save', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ channel: 'discord', config })
                    });
                    const data = await res.json();
                    if (data.status === 'ok') {
                        this.showToast('Discord settings saved!', 'success');
                        await this.getChannelStatus();
                        this.loadDiscordSettings();
                    } else {
                        this.showToast(data.error || 'Failed to save', 'error');
                    }
                } catch (e) {
                    this.showToast('Failed to save settings: ' + e.message, 'error');
                } finally {
                    this.channelLoading = false;
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
             * Toggle (start/stop) a channel adapter.
             * On "start", checks if the optional dep is installed first.
             */
            async toggleChannel(channel, skipDepCheck) {
                const isRunning = this.channelStatus[channel]?.running;
                const action = isRunning ? 'stop' : 'start';

                // WhatsApp business mode uses httpx (core dep) — skip dep check
                const needsDepCheck = action === 'start'
                    && !skipDepCheck
                    && !(channel === 'whatsapp' && this.channelStatus.whatsapp?.mode === 'business')
                    && !(channel === 'signal');

                if (needsDepCheck) {
                    try {
                        const res = await fetch(`/api/extras/check?channel=${encodeURIComponent(channel)}`);
                        if (res.ok) {
                            const info = await res.json();
                            if (!info.installed) {
                                this.installPrompt = {
                                    channel,
                                    package: info.package,
                                    pipSpec: info.pip_spec,
                                };
                                this.$nextTick(() => {
                                    if (window.refreshIcons) window.refreshIcons();
                                });
                                return;
                            }
                        }
                    } catch (e) {
                        // If check fails, proceed with toggle — backend will surface errors
                    }
                }

                this.channelLoading = true;
                try {
                    const res = await fetch('/api/channels/toggle', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ channel, action })
                    });
                    const data = await res.json();

                    if (data.missing_dep) {
                        // Backend detected a missing dependency — show install modal
                        this.installPrompt = {
                            channel: data.channel,
                            package: data.package,
                            pipSpec: data.pip_spec,
                        };
                        this.$nextTick(() => {
                            if (window.refreshIcons) window.refreshIcons();
                        });
                    } else if (data.error) {
                        this.showToast(data.error, 'error');
                    } else if (data.starting) {
                        // Adapter is starting in the background, poll until running
                        this._startingInBackground = true;
                        const label = channel.charAt(0).toUpperCase() + channel.slice(1);
                        this.showToast(`${label} starting...`, 'info');
                        this._pollChannelStart(channel);
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
                    this._startingInBackground = false;
                } finally {
                    // Keep loading spinner while polling a background start
                    if (!this._startingInBackground) {
                        this.channelLoading = false;
                    }
                    this.$nextTick(() => {
                        if (window.refreshIcons) window.refreshIcons();
                    });
                }
            },

            /**
             * Poll channel status until it's running or timeout (45s).
             */
            _pollChannelStart(channel, attempts = 0) {
                if (attempts > 15) {
                    const label = channel.charAt(0).toUpperCase() + channel.slice(1);
                    this.showToast(`${label} failed to start. Check logs.`, 'error');
                    this._startingInBackground = false;
                    this.channelLoading = false;
                    this.getChannelStatus();
                    return;
                }
                setTimeout(async () => {
                    await this.getChannelStatus();
                    if (this.channelStatus[channel]?.running) {
                        const label = channel.charAt(0).toUpperCase() + channel.slice(1);
                        this.showToast(`${label} started!`, 'success');
                        this._startingInBackground = false;
                        this.channelLoading = false;
                        if (channel === 'whatsapp') {
                            this.startWhatsAppQrPollingIfNeeded();
                        }
                    } else {
                        this._pollChannelStart(channel, attempts + 1);
                    }
                }, 3000);
            },

            /**
             * User confirmed installation of a missing dependency
             */
            async confirmInstall() {
                if (!this.installPrompt) return;
                const { channel } = this.installPrompt;
                this.installLoading = true;
                try {
                    const res = await fetch('/api/extras/install', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ extra: channel })
                    });
                    const data = await res.json();
                    if (data.error) {
                        this.showToast('Install failed: ' + data.error, 'error');
                    } else if (data.restart_required) {
                        // Installation succeeded but needs restart
                        const packageName = this.installPrompt.package;
                        this.installPrompt = null;
                        this.installLoading = false;

                        // Show restart confirmation via Alpine.js modal
                        this.showConfirm(
                            `${packageName} Installed`,
                            `The server must restart to load native extensions.\n\nRestart now? (You'll reconnect automatically)`,
                            async () => { await this.restartServerForChannel(channel); },
                            () => { this.showToast('Installation complete. Restart server when ready.', 'info'); }
                        );
                        return;
                    } else {
                        this.showToast(`${this.installPrompt.package} installed!`, 'success');
                        this.installPrompt = null;
                        // Retry starting the channel
                        await this.toggleChannel(channel, true);
                    }
                } catch (e) {
                    this.showToast('Install failed: ' + e.message, 'error');
                } finally {
                    this.installLoading = false;
                }
            },

            /**
             * User cancelled the install prompt
             */
            cancelInstall() {
                this.installPrompt = null;
                this.installLoading = false;
            },

            /**
             * Restart server after channel installation
             */
            async restartServerForChannel(channel) {
                try {
                    const res = await fetch('/api/system/restart', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ confirm: true })
                    });

                    if (!res.ok) {
                        const data = await res.json();
                        this.showToast(
                            `Failed to restart server: ${data.error || 'Unknown error'}`,
                            'error'
                        );
                        return;
                    }

                    const data = await res.json();
                    if (data.restarting) {
                        this.showToast(
                            'Server restarting. Reconnecting in a few seconds...',
                            'info'
                        );
                        // Store channel name to retry after reconnect
                        sessionStorage.setItem('pendingChannelStart', channel);
                    }
                } catch (e) {
                    this.showToast(
                        'Server restart initiated (connection lost)',
                        'info'
                    );
                    // Store channel name anyway - server might have restarted
                    sessionStorage.setItem('pendingChannelStart', channel);
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
                this.showConfirm(
                    'Remove Webhook',
                    `Remove webhook "${name}"?`,
                    async () => {
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
                    }
                );
            },

            /**
             * Regenerate a webhook slot's secret
             */
            async regenerateWebhookSecret(name) {
                this.showConfirm(
                    'Regenerate Secret',
                    `Regenerate secret for "${name}"? Existing integrations will break.`,
                    async () => {
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
                    }
                );
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

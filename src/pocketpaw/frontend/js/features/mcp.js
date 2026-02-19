/**
 * PocketPaw - MCP Servers Feature Module
 *
 * Created: 2026-02-07
 * Updated: 2026-02-17 — Removed Registry tab, added paste-command input.
 *
 * Manages MCP (Model Context Protocol) server connections:
 * - List/add/remove servers
 * - Enable/disable servers
 * - View tool inventory
 * - Browse & install presets from the curated catalog
 * - Paste a full command to auto-fill Add Server form
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.MCP = {
    name: 'MCP',
    /**
     * Get initial state for MCP
     */
    getState() {
        return {
            showMCP: false,
            mcpServers: {},
            mcpForm: {
                name: '',
                transport: 'stdio',
                command: '',
                args: '',
                url: '',
                fullCommand: ''
            },
            mcpLoading: false,
            mcpShowAddForm: false,
            mcpPresets: [],
            mcpView: 'servers',
            mcpInstallId: null,
            mcpInstallEnv: {},
            mcpInstallArgs: '',
            mcpInstalling: false,
            mcpInstallAbort: null,
            mcpCategoryFilter: 'all'
        };
    },

    /**
     * Get methods for MCP
     */
    getMethods() {
        return {
            /**
             * Open MCP modal and fetch status
             */
            async openMCP() {
                this.showMCP = true;
                await this.getMCPStatus();
                await this.loadPresets();

                // Register WS handler for OAuth redirect (once)
                if (!window.PocketPaw._mcpOAuthRegistered && window.socket) {
                    window.socket.on('mcp_oauth_redirect', (data) => {
                        if (!data.url) return;
                        // Navigate pre-opened popup or show fallback
                        const popup = window.PocketPaw._oauthPopup;
                        if (popup && !popup.closed) {
                            popup.location = data.url;
                        } else {
                            // Popup was blocked — show clickable link
                            const name = data.server || 'server';
                            if (this.showToast) {
                                this.showToast(
                                    `Open auth link for ${name}: ` +
                                    data.url.substring(0, 60) + '...',
                                    'info'
                                );
                            }
                            window.open(data.url, '_blank');
                        }
                    });
                    window.PocketPaw._mcpOAuthRegistered = true;
                }

                this.$nextTick(() => {
                    if (window.refreshIcons) window.refreshIcons();
                });
            },

            /**
             * Fetch MCP server status from backend
             */
            async getMCPStatus() {
                try {
                    const res = await fetch('/api/mcp/status');
                    if (res.ok) {
                        this.mcpServers = await res.json();
                    }
                } catch (e) {
                    console.error('Failed to get MCP status', e);
                }
            },

            /**
             * Add a new MCP server
             */
            async addMCPServer() {
                if (!this.mcpForm.name) return;
                this.mcpLoading = true;
                try {
                    const body = {
                        name: this.mcpForm.name,
                        transport: this.mcpForm.transport,
                        command: this.mcpForm.command,
                        args: this.mcpForm.args
                            ? this.mcpForm.args.split(',').map(s => s.trim())
                            : [],
                        url: this.mcpForm.url,
                        enabled: true
                    };
                    const res = await fetch('/api/mcp/add', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(body)
                    });
                    const data = await res.json();
                    if (data.status === 'ok') {
                        this.showToast(`MCP server "${this.mcpForm.name}" added`, 'success');
                        this.mcpForm = { name: '', transport: 'stdio', command: '', args: '', url: '', fullCommand: '' };
                        await this.getMCPStatus();
                    } else {
                        this.showToast(data.error || 'Failed to add server', 'error');
                    }
                } catch (e) {
                    this.showToast('Failed to add MCP server: ' + e.message, 'error');
                } finally {
                    this.mcpLoading = false;
                    this.$nextTick(() => {
                        if (window.refreshIcons) window.refreshIcons();
                    });
                }
            },

            /**
             * Remove an MCP server
             */
            async removeMCPServer(name) {
                try {
                    const res = await fetch('/api/mcp/remove', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ name })
                    });
                    const data = await res.json();
                    if (data.status === 'ok') {
                        this.showToast(`MCP server "${name}" removed`, 'info');
                        await this.getMCPStatus();
                        await this.loadPresets();
                    } else {
                        this.showToast(data.error || 'Failed to remove', 'error');
                    }
                } catch (e) {
                    this.showToast('Failed to remove server: ' + e.message, 'error');
                }
            },

            /**
             * Toggle an MCP server: start if stopped, stop if running
             */
            async toggleMCPServer(name) {
                try {
                    const res = await fetch('/api/mcp/toggle', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ name })
                    });
                    const data = await res.json();
                    if (data.status === 'ok') {
                        if (data.enabled) {
                            const msg = data.connected
                                ? `"${name}" connected`
                                : `"${name}" failed to connect`;
                            this.showToast(msg, data.connected ? 'success' : 'error');
                        } else {
                            this.showToast(`"${name}" stopped`, 'info');
                        }
                        await this.getMCPStatus();
                    } else {
                        this.showToast(data.error || 'Failed to toggle', 'error');
                    }
                } catch (e) {
                    this.showToast('Failed to toggle server: ' + e.message, 'error');
                }
                this.$nextTick(() => {
                    if (window.refreshIcons) window.refreshIcons();
                });
            },

            /**
             * Get the count of connected MCP servers (for sidebar badge)
             */
            connectedMCPCount() {
                return Object.values(this.mcpServers).filter(s => s.connected).length;
            },

            /**
             * Load presets from backend
             */
            async loadPresets() {
                try {
                    const res = await fetch('/api/mcp/presets');
                    if (res.ok) {
                        this.mcpPresets = await res.json();
                    }
                } catch (e) {
                    console.error('Failed to load MCP presets', e);
                }
            },

            /**
             * Cancel an in-progress install (abort fetch, reset state, close popup)
             */
            cancelInstall() {
                if (this.mcpInstallAbort) {
                    this.mcpInstallAbort.abort();
                    this.mcpInstallAbort = null;
                }
                this.mcpInstalling = false;
                this.mcpInstallId = null;
                const popup = window.PocketPaw._oauthPopup;
                if (popup && !popup.closed) {
                    try { popup.close(); } catch (_) { /* cross-origin */ }
                }
                window.PocketPaw._oauthPopup = null;
            },

            /**
             * Show install form for a preset
             */
            showInstallForm(presetId) {
                if (this.mcpInstallId === presetId) {
                    this.cancelInstall();
                    return;
                }
                this.mcpInstallId = presetId;
                this.mcpInstallArgs = '';
                const preset = this.mcpPresets.find(p => p.id === presetId);
                if (preset) {
                    const env = {};
                    for (const ek of preset.env_keys) {
                        env[ek.key] = '';
                    }
                    this.mcpInstallEnv = env;
                }
                this.$nextTick(() => {
                    if (window.refreshIcons) window.refreshIcons();
                });
            },

            /**
             * Install a preset
             */
            async installPreset() {
                if (!this.mcpInstallId) return;
                this.mcpInstalling = true;

                // AbortController so Cancel can kill the pending fetch
                const abort = new AbortController();
                this.mcpInstallAbort = abort;

                // For OAuth presets: open a blank popup NOW (in user click context)
                // so the browser allows it. The WS handler will navigate it later.
                const isOAuth = this.presetIsOAuth(this.mcpInstallId);
                if (isOAuth) {
                    window.PocketPaw._oauthPopup = window.open(
                        'about:blank', 'pocketpaw_oauth',
                        'width=600,height=700,scrollbars=yes'
                    );
                }

                try {
                    const body = {
                        preset_id: this.mcpInstallId,
                        env: this.mcpInstallEnv
                    };
                    const args = this.mcpInstallArgs.trim();
                    if (args) {
                        body.extra_args = args.split(/\s+/);
                    }
                    const res = await fetch('/api/mcp/presets/install', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(body),
                        signal: abort.signal
                    });
                    const data = await res.json();
                    if (res.ok && data.status === 'ok') {
                        const toolCount = data.tools ? data.tools.length : 0;
                        const msg = data.connected
                            ? `Installed — ${toolCount} tools discovered`
                            : 'Installed (not yet connected)';
                        this.showToast(msg, 'success');
                        this.mcpInstallId = null;
                        await this.getMCPStatus();
                        await this.loadPresets();
                    } else {
                        this.showToast(data.error || 'Install failed', 'error');
                    }
                } catch (e) {
                    if (e.name === 'AbortError') return; // User cancelled
                    this.showToast('Install failed: ' + e.message, 'error');
                } finally {
                    this.mcpInstalling = false;
                    this.mcpInstallAbort = null;
                    // Close leftover OAuth popup if still blank
                    const popup = window.PocketPaw._oauthPopup;
                    if (popup && !popup.closed) {
                        try {
                            if (popup.location.href === 'about:blank') {
                                popup.close();
                            }
                        } catch (_) { /* cross-origin — popup navigated, leave it */ }
                    }
                    window.PocketPaw._oauthPopup = null;
                    this.$nextTick(() => {
                        if (window.refreshIcons) window.refreshIcons();
                    });
                }
            },

            /**
             * Derive category list from loaded presets
             */
            mcpCategories() {
                const cats = new Set(this.mcpPresets.map(p => p.category));
                return ['all', ...Array.from(cats).sort()];
            },

            /**
             * Filter presets by selected category
             */
            filteredPresets() {
                if (this.mcpCategoryFilter === 'all') return this.mcpPresets;
                return this.mcpPresets.filter(p => p.category === this.mcpCategoryFilter);
            },

            /**
             * Check if a preset needs extra args (driven by backend needs_args flag)
             */
            presetNeedsArgs(presetId) {
                const preset = this.mcpPresets.find(p => p.id === presetId);
                return preset ? !!preset.needs_args : false;
            },

            /**
             * Check if a preset uses OAuth authentication
             */
            presetIsOAuth(presetId) {
                const preset = this.mcpPresets.find(p => p.id === presetId);
                return preset ? !!preset.oauth : false;
            },

            /**
             * Get button text for a preset based on OAuth status and install state
             */
            presetButtonText(presetId) {
                if (this.mcpInstallId === presetId) return 'Cancel';
                return this.presetIsOAuth(presetId) ? 'Authenticate' : 'Install';
            },

            /**
             * Parse a pasted full command string and auto-populate the Add Server form.
             * Handles patterns like:
             *   "npx -y @some/package"
             *   "uvx mcp-server-git"
             *   "docker run -i --rm ghcr.io/org/img"
             */
            parseFullCommand() {
                const raw = (this.mcpForm.fullCommand || '').trim();
                if (!raw) return;

                const parts = raw.split(/\s+/);
                if (parts.length === 0) return;

                const command = parts[0];
                const args = parts.slice(1);

                this.mcpForm.command = command;
                this.mcpForm.args = args.join(', ');

                // Auto-derive a name from the last arg that looks like a package
                let name = '';
                for (let i = args.length - 1; i >= 0; i--) {
                    const a = args[i];
                    // Skip flags and version suffixes
                    if (a.startsWith('-')) continue;
                    // Use the package-like arg
                    name = a
                        .replace(/@latest$/, '')
                        .replace(/@[\d.]+.*$/, '');
                    // Extract short name: "@scope/pkg" -> "pkg", "mcp-server-git" -> "mcp-server-git"
                    if (name.includes('/')) {
                        name = name.split('/').pop() || name;
                    }
                    name = name.replace(/^@/, '');
                    break;
                }

                if (name && !this.mcpForm.name) {
                    this.mcpForm.name = name;
                }
            }
        };
    }
};

window.PocketPaw.Loader.register('MCP', window.PocketPaw.MCP);

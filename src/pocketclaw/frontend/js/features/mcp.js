/**
 * PocketPaw - MCP Servers Feature Module
 *
 * Created: 2026-02-07
 * Updated: 2026-02-12 — Registry tab (browse official MCP registry), dynamic categories, needs_args.
 *
 * Manages MCP (Model Context Protocol) server connections:
 * - List/add/remove servers
 * - Enable/disable servers
 * - View tool inventory
 * - Browse & install presets from the catalog
 * - Search & install from the official MCP Registry (16K+ servers)
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
                url: ''
            },
            mcpLoading: false,
            mcpShowAddForm: false,
            mcpPresets: [],
            mcpView: 'servers',
            mcpInstallId: null,
            mcpInstallEnv: {},
            mcpInstallArgs: '',
            mcpInstalling: false,
            mcpCategoryFilter: 'all',
            // Registry state
            mcpRegistryQuery: '',
            mcpRegistryResults: [],
            mcpRegistryFeatured: [],
            mcpRegistryLoading: false,
            mcpRegistryFeaturedError: false,
            mcpRegistryCursor: null,
            mcpRegistryLoadingMore: false,
            mcpRegistryInstalling: null,
            mcpRegistryInstallEnv: {}
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
                        this.mcpForm = { name: '', transport: 'stdio', command: '', args: '', url: '' };
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
             * Show install form for a preset
             */
            showInstallForm(presetId) {
                if (this.mcpInstallId === presetId) {
                    this.mcpInstallId = null;
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
                        body: JSON.stringify(body)
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
                    this.showToast('Install failed: ' + e.message, 'error');
                } finally {
                    this.mcpInstalling = false;
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

            // ==================== Registry Methods ====================

            /**
             * Search the official MCP Registry (debounced via Alpine @input.debounce)
             */
            async searchRegistry() {
                const q = this.mcpRegistryQuery.trim();
                if (!q) {
                    this.mcpRegistryResults = [];
                    this.mcpRegistryCursor = null;
                    return;
                }

                this.mcpRegistryLoading = true;
                try {
                    const url = `/api/mcp/registry/search?q=${encodeURIComponent(q)}&limit=30`;
                    const res = await fetch(url);
                    if (res.ok) {
                        const data = await res.json();
                        this.mcpRegistryResults = data.servers || [];
                        this.mcpRegistryCursor = data.metadata?.nextCursor || null;
                    }
                } catch (e) {
                    console.error('Registry search failed', e);
                } finally {
                    this.mcpRegistryLoading = false;
                    this.$nextTick(() => {
                        if (window.refreshIcons) window.refreshIcons();
                    });
                }
            },

            /**
             * Load featured/popular registry servers for initial view
             */
            async loadRegistryFeatured() {
                if (this.mcpRegistryFeatured.length > 0) return;
                this.mcpRegistryLoading = true;
                this.mcpRegistryFeaturedError = false;
                try {
                    const res = await fetch('/api/mcp/registry/search?limit=30');
                    if (res.ok) {
                        const data = await res.json();
                        this.mcpRegistryFeatured = data.servers || [];
                        if (data.error) {
                            this.mcpRegistryFeaturedError = true;
                        }
                    } else {
                        this.mcpRegistryFeaturedError = true;
                    }
                } catch (e) {
                    console.error('Failed to load registry featured', e);
                    this.mcpRegistryFeaturedError = true;
                } finally {
                    this.mcpRegistryLoading = false;
                    this.$nextTick(() => {
                        if (window.refreshIcons) window.refreshIcons();
                    });
                }
            },

            /**
             * Retry loading featured servers (clears cache first)
             */
            async retryRegistryFeatured() {
                this.mcpRegistryFeatured = [];
                await this.loadRegistryFeatured();
            },

            /**
             * Load more registry results (pagination)
             */
            async loadMoreRegistry() {
                if (!this.mcpRegistryCursor || this.mcpRegistryLoadingMore) return;
                this.mcpRegistryLoadingMore = true;
                try {
                    const q = this.mcpRegistryQuery.trim();
                    let url = `/api/mcp/registry/search?limit=30&cursor=${encodeURIComponent(this.mcpRegistryCursor)}`;
                    if (q) url += `&q=${encodeURIComponent(q)}`;
                    const res = await fetch(url);
                    if (res.ok) {
                        const data = await res.json();
                        const newServers = data.servers || [];
                        this.mcpRegistryResults = [...this.mcpRegistryResults, ...newServers];
                        this.mcpRegistryCursor = data.metadata?.nextCursor || null;
                    }
                } catch (e) {
                    console.error('Registry load more failed', e);
                } finally {
                    this.mcpRegistryLoadingMore = false;
                    this.$nextTick(() => {
                        if (window.refreshIcons) window.refreshIcons();
                    });
                }
            },

            /**
             * Get the list to display in registry view
             */
            registryDisplayResults() {
                return this.mcpRegistryQuery.trim()
                    ? this.mcpRegistryResults
                    : this.mcpRegistryFeatured;
            },

            /**
             * Extract a short display name from a registry server
             */
            registryServerName(server) {
                if (server.title) return server.title;
                const name = server.name || '';
                return name.includes('/') ? name.split('/').pop() : name;
            },

            /**
             * Extract a source label (e.g. "npm: @mcp/server" or "HTTP")
             */
            registryServerSource(server) {
                const remotes = server.remotes || [];
                const packages = server.packages || [];
                if (remotes.length > 0) return 'HTTP';
                if (packages.length > 0) {
                    const pkg = packages[0];
                    const type = pkg.registryType || 'npm';
                    return `${type}: ${pkg.name || ''}`;
                }
                return server.name || '';
            },

            /**
             * Check if a registry server is already installed locally
             */
            isRegistryServerInstalled(server) {
                const rawName = server.name || '';
                const installed = Object.keys(this.mcpServers).map(n => n.toLowerCase());
                // Check both the full derived name and the simple last-segment name
                const parts = rawName.split('/');
                const lastPart = (parts.pop() || '').toLowerCase();
                const orgPart = parts.length > 0
                    ? (parts[0].includes('.') ? parts[0].split('.').pop() : parts[0]).replace(/^@/, '').toLowerCase()
                    : '';
                const generic = ['mcp', 'server', 'mcp-server', 'main', 'app', 'api'];
                const derivedName = generic.includes(lastPart) && orgPart
                    ? `${orgPart}-${lastPart}`
                    : lastPart;
                return installed.includes(derivedName) || installed.includes(lastPart);
            },

            /**
             * Get env vars required by a registry server
             */
            registryServerEnvVars(server) {
                return (server.environmentVariables || []).filter(ev => ev.required !== false);
            },

            /**
             * Show install form for a registry server
             */
            showRegistryInstallForm(serverName) {
                if (this.mcpRegistryInstalling === serverName) {
                    this.mcpRegistryInstalling = null;
                    return;
                }
                this.mcpRegistryInstalling = serverName;
                // Pre-fill env
                const results = this.registryDisplayResults();
                const server = results.find(s => s.name === serverName);
                const env = {};
                if (server) {
                    for (const ev of (server.environmentVariables || [])) {
                        env[ev.name] = '';
                    }
                }
                this.mcpRegistryInstallEnv = env;
                this.$nextTick(() => {
                    if (window.refreshIcons) window.refreshIcons();
                });
            },

            /**
             * Install a server from the registry
             */
            async installFromRegistry(server) {
                const serverName = server.name;
                this.mcpRegistryInstalling = serverName;
                try {
                    const res = await fetch('/api/mcp/registry/install', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            server: server,
                            env: this.mcpRegistryInstallEnv
                        })
                    });
                    const data = await res.json();
                    if (res.ok && data.status === 'ok') {
                        const toolCount = data.tools ? data.tools.length : 0;
                        let msg;
                        if (data.connected) {
                            msg = `Installed "${data.name}" — ${toolCount} tools`;
                        } else {
                            msg = `Installed "${data.name}" (not yet connected)`;
                            if (data.error) msg += `: ${data.error}`;
                        }
                        this.showToast(msg, data.connected ? 'success' : 'warning');
                        this.mcpRegistryInstalling = null;
                        await this.getMCPStatus();
                    } else {
                        this.showToast(data.error || 'Install failed', 'error');
                        this.mcpRegistryInstalling = null;
                    }
                } catch (e) {
                    this.showToast('Install failed: ' + e.message, 'error');
                    this.mcpRegistryInstalling = null;
                }
                this.$nextTick(() => {
                    if (window.refreshIcons) window.refreshIcons();
                });
            }
        };
    }
};

window.PocketPaw.Loader.register('MCP', window.PocketPaw.MCP);

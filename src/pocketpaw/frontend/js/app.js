/**
 * PocketPaw Main Application
 * Alpine.js component for the dashboard
 *
 * Changes (2026-02-17):
 * - Health reconnect hook: re-fetch health data on WS reconnect
 * - Added health_update socket handler and get_health on connect
 *
 * Changes (2026-02-12):
 * - Call initHashRouter() in init() for hash-based URL routing
 *
 * Changes (2026-02-05):
 * - MAJOR REFACTOR: Componentized into feature modules using mixin pattern
 * - Extracted features to js/features/: chat, file-browser, reminders, intentions,
 *   skills, transparency, remote-access, mc-agents, mc-tasks, deep-work, mc-events
 * - This file now serves as the core assembler for feature modules
 * - Core functionality: init, WebSocket setup, settings, status, tools, logging
 *
 * Previous changes preserved in feature module files.
 */

function app() {
    // Assemble all registered feature modules via Loader
    const { state: featureStates, methods: featureMethods } =
        window.PocketPaw.Loader.assemble();

    return {
        // ==================== Core State ====================

        // Version & updates
        appVersion: '',
        latestVersion: '',
        updateAvailable: false,

        // View state
        view: 'chat',
        showSettings: false,
        showWelcome: false,
        showScreenshot: false,
        screenshotSrc: '',

        // Login gate
        showLogin: false,
        loginToken: '',
        loginError: '',
        loginLoading: false,

        // Settings panel state
        settingsSection: 'general',
        settingsMobileView: 'list',
        settingsSearch: '',
        settingsSearchResults: [],
        settingsSections: [
            { id: 'general', label: 'General', icon: 'settings' },
            { id: 'apikeys', label: 'API Keys', icon: 'key' },
            { id: 'behavior', label: 'Behavior & Safety', icon: 'brain' },
            { id: 'memory', label: 'Memory', icon: 'database' },
            { id: 'services', label: 'Search & Services', icon: 'search' },
            { id: 'system', label: 'System', icon: 'activity' },
        ],

        // Terminal logs
        logs: [],

        // System status
        status: {
            cpu: '...',
            ram: '...',
            disk: '...',
            battery: '...'
        },

        // Settings
        settings: {
            agentBackend: 'claude_agent_sdk',
            claudeSdkModel: '',
            claudeSdkMaxTurns: 0,
            openaiAgentsModel: '',
            openaiAgentsMaxTurns: 0,
            googleAdkModel: 'gemini-3-pro-preview',
            googleAdkMaxTurns: 0,
            codexCliModel: 'gpt-5.3-codex',
            codexCliMaxTurns: 0,
            opencodeBaseUrl: 'http://localhost:4096',
            opencodeModel: '',
            opencodeMaxTurns: 0,
            claudeSdkProvider: 'anthropic',
            openaiAgentsProvider: 'openai',
            llmProvider: 'auto',
            ollamaHost: 'http://localhost:11434',
            ollamaModel: 'llama3.2',
            anthropicModel: 'claude-sonnet-4-6',
            openaiCompatibleBaseUrl: '',
            openaiCompatibleApiKey: '',
            openaiCompatibleModel: '',
            openaiCompatibleMaxTokens: 0,
            geminiModel: 'gemini-3-pro-preview',
            bypassPermissions: false,
            webSearchProvider: 'tavily',
            urlExtractProvider: 'auto',
            injectionScanEnabled: true,
            injectionScanLlm: false,
            toolProfile: 'full',
            planMode: false,
            planModeTools: 'shell,write_file,edit_file',
            smartRoutingEnabled: false,
            modelTierSimple: 'claude-haiku-4-5-20251001',
            modelTierModerate: 'claude-sonnet-4-6',
            modelTierComplex: 'claude-opus-4-6',
            ttsProvider: 'openai',
            ttsVoice: 'alloy',
            sttProvider: 'openai',
            sttModel: 'whisper-1',
            ocrProvider: 'openai',
            sarvamTtsLanguage: 'hi-IN',
            selfAuditEnabled: true,
            selfAuditSchedule: '0 3 * * *',
            memoryBackend: 'file',
            mem0AutoLearn: true,
            mem0LlmProvider: 'anthropic',
            mem0LlmModel: 'claude-haiku-4-5-20251001',
            mem0EmbedderProvider: 'openai',
            mem0EmbedderModel: 'text-embedding-3-small',
            mem0VectorStore: 'qdrant',
            mem0OllamaBaseUrl: 'http://localhost:11434',
            webHost: '127.0.0.1',
            webPort: 8888
        },

        // API Keys (not persisted client-side, but we track if saved on server)
        apiKeys: {
            anthropic: '',
            openai: '',
            google: '',
            tavily: '',
            brave: '',
            parallel: '',
            elevenlabs: '',
            google_oauth_id: '',
            google_oauth_secret: '',
            spotify_client_id: '',
            spotify_client_secret: '',
            sarvam: ''
        },
        hasAnthropicKey: false,
        hasOpenaiKey: false,
        hasOpenaiCompatibleKey: false,
        hasGoogleApiKey: false,
        hasTavilyKey: false,
        hasBraveKey: false,
        hasParallelKey: false,
        hasElevenlabsKey: false,
        hasGoogleOAuthId: false,
        hasGoogleOAuthSecret: false,
        hasSpotifyClientId: false,
        hasSpotifyClientSecret: false,
        hasSarvamKey: false,

        // Backend discovery data (fetched from /api/backends)
        _backendsData: [],
        backendInstallLoading: false,
        serverRestarting: false,

        // Spread feature states
        ...featureStates,

        // ==================== Core Methods ====================

        /**
         * Initialize the app
         */
        /**
         * Validate token via /api/auth/login (sets HTTP-only cookie) and reload.
         */
        async submitToken() {
            const token = this.loginToken.trim();
            if (!token) return;
            this.loginError = '';
            this.loginLoading = true;
            try {
                const resp = await fetch('/api/auth/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ token }),
                });
                if (resp.ok) {
                    window.location.reload();
                } else {
                    this.loginError = 'Invalid access token. Please try again.';
                }
            } catch {
                this.loginError = 'Connection failed. Is PocketPaw running?';
            } finally {
                this.loginLoading = false;
            }
        },

        init() {
            this.log('PocketPaw Dashboard initialized', 'info');

            // Handle Auth Token (URL capture → exchange for session token)
            const urlParams = new URLSearchParams(window.location.search);
            const masterToken = urlParams.get('token');
            if (masterToken) {
                // Clean URL immediately (don't leave master token visible)
                window.history.replaceState({}, document.title, window.location.pathname);
                // Store master token immediately as fallback
                localStorage.setItem('pocketpaw_token', masterToken);
                // Exchange master token for a time-limited session token (async)
                fetch('/api/auth/session', {
                    method: 'POST',
                    headers: { 'Authorization': `Bearer ${masterToken}` }
                }).then(resp => {
                    if (resp.ok) return resp.json();
                    return null;
                }).then(data => {
                    if (data && data.session_token) {
                        localStorage.setItem('pocketpaw_token', data.session_token);
                        this.log('Session token obtained', 'success');
                    } else {
                        this.log('Auth token captured', 'info');
                    }
                }).catch(() => {
                    this.log('Auth token captured (session exchange unavailable)', 'info');
                });
            }

            // --- OVERRIDE FETCH FOR AUTH ---
            const originalFetch = window.fetch;
            const appRef = this;
            window.fetch = async (url, options = {}) => {
                const storedToken = localStorage.getItem('pocketpaw_token');

                // Skip auth for static or external
                if (url.toString().startsWith('/api') || url.toString().startsWith('/')) {
                    options.headers = options.headers || {};
                    if (storedToken) {
                        options.headers['Authorization'] = `Bearer ${storedToken}`;
                    }
                }

                const response = await originalFetch(url, options);

                if (response.status === 401 || response.status === 403) {
                    localStorage.removeItem('pocketpaw_token');
                    // Show login overlay if not already visible
                    if (!appRef.showLogin) {
                        appRef.showLogin = true;
                    }
                }

                return response;
            };

            // If no token is stored and none was captured from the URL,
            // probe the API to check if localhost auth bypass is active.
            // If not (e.g. Docker), show the login overlay instead of
            // attempting WS/API connections that will fail with 401.
            const hasToken = !!localStorage.getItem('pocketpaw_token');
            if (!hasToken && !masterToken) {
                originalFetch('/api/channels/status').then(resp => {
                    if (resp.ok) {
                        // Localhost bypass is active — proceed normally
                        this._startApp();
                    } else {
                        // Auth required — show login
                        this.showLogin = true;
                        this.$nextTick(() => {
                            if (this.$refs.tokenInput) this.$refs.tokenInput.focus();
                        });
                    }
                }).catch(() => {
                    this.showLogin = true;
                });
                return;
            }

            this._startApp();
        },

        /**
         * Start the app after auth is confirmed.
         * Connects WebSocket, loads sessions, sets up keyboard shortcuts.
         */
        _startApp() {
            // Wire EventBus listeners (cross-module communication)
            PocketPaw.EventBus.on('sidebar:files', (data) => this.handleSidebarFiles(data));
            PocketPaw.EventBus.on('output:files', (data) => this.handleOutputFiles(data));

            // Register event handlers first
            this.setupSocketHandlers();

            // Connect WebSocket (singleton - will only connect once)
            const lastSession = StateManager.load('lastSession');
            socket.connect(lastSession);

            // Load sessions for sidebar
            this.loadSessions();
            if (this.loadIdentityData) { 
                this.loadIdentityData(); 
            }

            // Start status polling (low frequency)
            this.startStatusPolling();

            // Keyboard shortcuts
            document.addEventListener('keydown', (e) => {
                // Cmd/Ctrl+N: New chat
                if ((e.metaKey || e.ctrlKey) && e.key === 'n') {
                    e.preventDefault();
                    this.createNewChat();
                }
                // Cmd/Ctrl+K: Focus search
                if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
                    e.preventDefault();
                    const searchInput = document.querySelector('.session-search-input');
                    if (searchInput) searchInput.focus();
                }
                // Cmd/Ctrl+,: Open settings
                if ((e.metaKey || e.ctrlKey) && e.key === ',') {
                    e.preventDefault();
                    this.openSettings();
                }
                // Escape: Cancel rename
                if (e.key === 'Escape' && this.editingSessionId) {
                    this.cancelRenameSession();
                }
            });

            // Fetch backends early (needed before wizard renders)
            fetch('/api/backends').then(r => r.json()).then(b => {
                this._backendsData = b;
            }).catch(() => {});

            // Check for version updates
            this.checkForUpdates();

            // Initialize hash-based URL routing
            this.initHashRouter();

            // Refresh Lucide icons after initial render
            this.$nextTick(() => {
                if (window.refreshIcons) window.refreshIcons();
            });
        },

        /**
         * Check PyPI for newer version via /api/version endpoint.
         */
        async checkForUpdates() {
            try {
                const resp = await fetch('/api/version');
                if (!resp.ok) return;
                const data = await resp.json();
                this.appVersion = data.current || '';
                this.latestVersion = data.latest || '';
                this.updateAvailable = !!data.update_available;
            } catch (e) { /* silent */ }
        },

        /**
         * Set up WebSocket event handlers
         */
        setupSocketHandlers() {
            // Clear existing handlers to prevent duplicates
            socket.clearHandlers();

            const onConnected = () => {
                this.log('Connected to PocketPaw Engine', 'success');
                // Fetch initial status and settings
                socket.runTool('status');
                socket.send('get_settings');

                // Fetch initial data for sidebar badges
                socket.send('get_reminders');
                socket.send('get_intentions');
                socket.send('get_skills');
                socket.send('get_health');

                // Re-fetch full health data if modal is open (handles server restart)
                if (this.onHealthReconnect) this.onHealthReconnect();

                // Resume last session if WS connect didn't handle it via query param
                const lastSession = StateManager.load('lastSession');
                if (lastSession && !this.currentSessionId) {
                    this.selectSession(lastSession);
                }

                // Auto-activate agent mode
                if (this.agentActive) {
                    socket.toggleAgent(true);
                    this.log('Agent Mode auto-activated', 'info');
                }
            };

            socket.on('connected', onConnected);

            // If already connected, trigger manually
            if (socket.isConnected) {
                onConnected();
            }

            socket.on('disconnected', () => {
                this.log('Disconnected from server', 'error');
            });

            socket.on('message', (data) => this.handleMessage(data));
            socket.on('notification', (data) => this.handleNotification(data));
            socket.on('status', (data) => this.handleStatus(data));
            socket.on('screenshot', (data) => this.handleScreenshot(data));
            socket.on('code', (data) => this.handleCode(data));
            socket.on('error', (data) => this.handleError(data));
            socket.on('stream_start', () => this.startStreaming());
            socket.on('stream_end', () => this.endStreaming());
            socket.on('files', (data) => this.handleFiles(data));
            socket.on('settings', (data) => this.handleSettings(data));

            // Reminder handlers
            socket.on('reminders', (data) => this.handleReminders(data));
            socket.on('reminder_added', (data) => this.handleReminderAdded(data));
            socket.on('reminder_deleted', (data) => this.handleReminderDeleted(data));
            socket.on('reminder', (data) => this.handleReminderTriggered(data));

            // Intention handlers
            socket.on('intentions', (data) => this.handleIntentions(data));
            socket.on('intention_created', (data) => this.handleIntentionCreated(data));
            socket.on('intention_updated', (data) => this.handleIntentionUpdated(data));
            socket.on('intention_toggled', (data) => this.handleIntentionToggled(data));
            socket.on('intention_deleted', (data) => this.handleIntentionDeleted(data));
            socket.on('intention_event', (data) => this.handleIntentionEvent(data));

            // Skills handlers
            socket.on('skills', (data) => this.handleSkills(data));
            socket.on('skill_started', (data) => this.handleSkillStarted(data));
            socket.on('skill_completed', (data) => this.handleSkillCompleted(data));
            socket.on('skill_received', (data) => console.log('Skill received', data));
            socket.on('skill_error', (data) => this.handleSkillError(data));

            // Transparency handlers
            socket.on('connection_info', (data) => this.handleConnectionInfo(data));
            socket.on('system_event', (data) => this.handleSystemEvent(data));

            // Health
            socket.on('health_update', (data) => this.handleHealthUpdate(data));

            // Session handlers
            socket.on('session_history', (data) => this.handleSessionHistory(data));
            socket.on('new_session', (data) => this.handleNewSession(data));

            // Note: Mission Control events come through system_event
            // They are handled in handleSystemEvent based on event_type prefix 'mc_'
        },

        /**
         * Handle status updates
         */
        handleStatus(data) {
            if (data.content) {
                this.status = Tools.parseStatus(data.content);
            }
        },

        /**
         * Handle settings from server (on connect)
         */
        handleSettings(data) {
            if (!data.content) return;
            const s = data.content;

            // Data-driven settings sync: map server keys to local settings
            const SETTINGS_MAP = [
                'agentBackend', 'claudeSdkProvider', 'claudeSdkModel', 'claudeSdkMaxTurns',
                'openaiAgentsProvider',
                'llmProvider', 'ollamaHost', 'ollamaModel', 'anthropicModel',
                'openaiCompatibleBaseUrl', 'openaiCompatibleModel', 'openaiCompatibleMaxTokens',
                'geminiModel',
                'bypassPermissions', 'webSearchProvider', 'urlExtractProvider',
                'injectionScanEnabled', 'injectionScanLlm', 'toolProfile',
                'planMode', 'planModeTools', 'smartRoutingEnabled',
                'modelTierSimple', 'modelTierModerate', 'modelTierComplex',
                'ttsProvider', 'ttsVoice', 'sttProvider', 'sttModel',
                'ocrProvider', 'sarvamTtsLanguage',
                'selfAuditEnabled', 'selfAuditSchedule',
                'memoryBackend', 'mem0AutoLearn', 'mem0LlmProvider',
                'mem0LlmModel', 'mem0EmbedderProvider', 'mem0EmbedderModel',
                'mem0VectorStore', 'mem0OllamaBaseUrl',
                'webHost', 'webPort'
            ];
            for (const key of SETTINGS_MAP) {
                if (s[key] !== undefined) this.settings[key] = s[key];
            }

            // API key availability flags
            const KEY_FLAGS = {
                hasAnthropicKey: false, hasOpenaiKey: false, hasOpenaiCompatibleKey: false,
                hasGoogleApiKey: false,
                hasTavilyKey: false, hasBraveKey: false,
                hasParallelKey: false, hasElevenlabsKey: false,
                hasGoogleOAuthId: false, hasGoogleOAuthSecret: false,
                hasSpotifyClientId: false, hasSpotifyClientSecret: false,
                hasSarvamKey: false
            };
            for (const flag of Object.keys(KEY_FLAGS)) {
                this[flag] = s[flag] || false;
            }

            // Log agent status if available
            if (s.agentStatus) {
                this.log(`Agent: ${s.agentStatus.backend} (available: ${s.agentStatus.available})`, 'info');
                if (s.agentStatus.features?.length > 0) {
                    this.log(`Features: ${s.agentStatus.features.join(', ')}`, 'info');
                }
            }

            // First-run welcome: show if backend unconfigured and not previously dismissed
            if (this.isBackendUnconfigured() && !localStorage.getItem('pocketpaw_setup_dismissed')) {
                this.showWelcome = true;
            }
        },

        /**
         * Handle screenshot
         */
        handleScreenshot(data) {
            if (data.image) {
                this.screenshotSrc = `data:image/png;base64,${data.image}`;
                this.showScreenshot = true;
            }
        },

        /**
         * Handle errors
         */
        handleError(data) {
            const content = data.content || 'Unknown error';
            this.addMessage('assistant', '❌ ' + content);
            this.log(content, 'error');
            this.showToast(content, 'error');
            this.endStreaming();

            // If file browser is open, show error there
            if (this.showFileBrowser) {
                this.fileLoading = false;
                this.fileError = content;
            }
        },

        /**
         * Run a tool
         */
        runTool(tool) {
            this.log(`Running tool: ${tool}`, 'info');

            // Special handling for file browser
            if (tool === 'fetch') {
                this.openFileBrowser();
                return;
            }

            socket.runTool(tool);
        },

        /**
         * Open settings modal (resets mobile view)
         */
        openSettings() {
            this.settingsMobileView = 'list';
            this.settingsSearch = '';
            this.settingsSearchResults = [];
            this.showSettings = true;
        },

        /**
         * Save settings
         */
        saveSettings() {
            socket.saveSettings(this.settings);
        },

        /**
         * Restart the server (for host/port changes)
         */
        async restartServer() {
            if (!confirm(
                'Restart the server? Active connections will be interrupted.'
            )) return;
            this.serverRestarting = true;
            try {
                await fetch('/api/system/restart', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ confirm: true }),
                });
                this.showToast(
                    'Server is restarting. If you changed the host or port, ' +
                    'reconnect at the new address.',
                    'info'
                );
            } catch {
                this.showToast('Restart request sent. Reconnecting…', 'info');
            } finally {
                // Reset after a delay so the button re-enables if the page survives
                setTimeout(() => { this.serverRestarting = false; }, 5000);
            }
        },

        /**
         * Save API key
         */
        saveApiKey(provider) {
            const key = this.apiKeys[provider];
            if (!key) {
                this.showToast('Please enter an API key', 'error');
                return;
            }

            socket.saveApiKey(provider, key);
            this.apiKeys[provider] = ''; // Clear input

            // Update local hasKey flags immediately
            const keyMap = {
                'anthropic': 'hasAnthropicKey',
                'openai': 'hasOpenaiKey',
                'tavily': 'hasTavilyKey',
                'brave': 'hasBraveKey',
                'parallel': 'hasParallelKey',
                'elevenlabs': 'hasElevenlabsKey',
                'google': 'hasGoogleApiKey',
                'google_oauth_id': 'hasGoogleOAuthId',
                'google_oauth_secret': 'hasGoogleOAuthSecret',
                'spotify_client_id': 'hasSpotifyClientId',
                'spotify_client_secret': 'hasSpotifyClientSecret',
                'sarvam': 'hasSarvamKey'
            };
            if (keyMap[provider]) {
                this[keyMap[provider]] = true;
            }

            this.log(`Saved ${provider} API key`, 'success');
            this.showToast(`${provider.charAt(0).toUpperCase() + provider.slice(1)} API key saved!`, 'success');

            // Refresh settings from backend to confirm key was persisted
            setTimeout(() => socket.send('get_settings'), 500);
        },

        /**
         * Start polling for system status (every 10 seconds, only when connected)
         */
        startStatusPolling() {
            setInterval(() => {
                if (socket.isConnected) {
                    socket.runTool('status');
                }
            }, 10000); // Poll every 10 seconds, not 3
        },

        /**
         * Add log entry
         */
        log(message, level = 'info') {
            this.logs.push({
                time: Tools.formatTime(),
                message,
                level
            });

            // Keep only last 100 logs
            if (this.logs.length > 100) {
                this.logs.shift();
            }

            // Auto scroll terminal
            this.$nextTick(() => {
                if (this.$refs.terminal) {
                    this.$refs.terminal.scrollTop = this.$refs.terminal.scrollHeight;
                }
            });
        },

        /**
         * Format message content
         */
        formatMessage(content) {
            return Tools.formatMessage(content);
        },

        /**
         * Get friendly label for current agent mode (shown in top bar)
         */
        getAgentModeLabel() {
            const labels = {
                'claude_agent_sdk': '🚀 Claude SDK',
                'openai_agents': '🤖 OpenAI Agents',
                'google_adk': '🔷 Google ADK',
                'opencode': '⌨️ OpenCode'
            };
            return labels[this.settings.agentBackend] || this.settings.agentBackend;
        },

        /**
         * Get description for each backend (shown in settings)
         */
        getBackendDescription(backend) {
            const descriptions = {
                'claude_agent_sdk': 'Built-in tools: Bash, WebSearch, WebFetch, Read, Write, Edit, Glob, Grep. Works with Anthropic & Ollama.',
                'openai_agents': 'OpenAI Agents SDK with code interpreter and file search. Works with OpenAI, Ollama, and local LLMs.',
                'google_adk': 'Google ADK — native Gemini agent with built-in tools: Google Search, code execution, MCP support.',
                'opencode': 'OpenCode AI coding agent. Requires server at configured URL.'
            };
            return descriptions[backend] || '';
        },

        /**
         * Check if the currently selected backend is available (installed).
         */
        isCurrentBackendAvailable() {
            const b = this._backendsData.find(x => x.name === this.settings.agentBackend);
            return b ? b.available : true;
        },

        /**
         * Get install hint for the currently selected backend.
         */
        currentBackendInstallHint() {
            const b = this._backendsData.find(x => x.name === this.settings.agentBackend);
            return (b && b.installHint) || {};
        },

        /**
         * Install the currently selected backend via POST /api/backends/install.
         */
        async installBackend() {
            this.backendInstallLoading = true;
            try {
                const resp = await fetch('/api/backends/install', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ backend: this.settings.agentBackend }),
                });
                const data = await resp.json();
                if (data.error) {
                    this.showToast(data.error, 'error');
                } else {
                    this.showToast('Backend installed successfully!', 'success');
                    // Re-fetch backends to update availability
                    const r = await fetch('/api/backends');
                    if (r.ok) {
                        this._backendsData = await r.json();
                        this.$nextTick(() => { if (window.refreshIcons) window.refreshIcons(); });
                    }
                }
            } catch (e) {
                this.showToast('Install failed: ' + e.message, 'error');
            } finally {
                this.backendInstallLoading = false;
            }
        },

        /**
         * Copy text to clipboard and show toast.
         */
        copyToClipboard(text) {
            navigator.clipboard.writeText(text).then(() => {
                this.showToast('Copied to clipboard', 'success');
            }).catch(() => {});
        },

        /**
         * Check if the current backend + sub-provider needs a specific API key.
         * Used by the API Keys section to show/hide key fields dynamically.
         */
        backendNeedsKey(keyField) {
            const backend = this.settings.agentBackend;
            // Get sub-provider for backends that support it
            let provider = '';
            if (backend === 'claude_agent_sdk') provider = this.settings.claudeSdkProvider || 'anthropic';
            else if (backend === 'openai_agents') provider = this.settings.openaiAgentsProvider || 'openai';
            else if (backend === 'google_adk') provider = 'google';
            else if (backend === 'codex_cli') provider = 'openai';
            else if (backend === 'opencode') return false;
            else if (backend === 'copilot_sdk') return false;

            // Ollama and openai_compatible don't need top-level API keys
            if (provider === 'ollama' || provider === 'openai_compatible') return false;

            // Map backend+provider to required key
            const keyMap = {
                'claude_agent_sdk:anthropic': 'anthropic_api_key',
                'openai_agents:openai': 'openai_api_key',
                'google_adk:google': 'google_api_key',
                'codex_cli:openai': 'openai_api_key',
            };
            return keyMap[backend + ':' + provider] === keyField;
        },

        /**
         * Check if the current backend+provider needs an API key that isn't saved.
         */
        isBackendUnconfigured() {
            const backend = this.settings.agentBackend;
            let provider = '';
            if (backend === 'claude_agent_sdk') provider = this.settings.claudeSdkProvider || 'anthropic';
            else if (backend === 'openai_agents') provider = this.settings.openaiAgentsProvider || 'openai';
            else if (backend === 'google_adk') provider = 'google';
            else if (backend === 'codex_cli') provider = 'openai';
            else return false; // opencode, copilot_sdk don't need keys

            if (provider === 'ollama' || provider === 'openai_compatible') return false;

            const needsMap = {
                'claude_agent_sdk:anthropic': 'hasAnthropicKey',
                'openai_agents:openai': 'hasOpenaiKey',
                'google_adk:google': 'hasGoogleApiKey',
                'codex_cli:openai': 'hasOpenaiKey',
            };
            const flag = needsMap[backend + ':' + provider];
            return flag ? !this[flag] : false;
        },

        /**
         * Return CSS dot class for settings nav items (red=needs attention, green=ok, empty=neutral).
         */
        getSettingsNavDot(sectionId) {
            if (sectionId === 'general' || sectionId === 'apikeys') {
                return this.isBackendUnconfigured() ? 'bg-[var(--danger-color)]' : 'bg-[var(--success-color)]';
            }
            return '';
        },

        // ---- Settings search ----
        _SETTINGS_INDEX: [
            { section: 'general', sectionLabel: 'General', label: 'Agent Backend', hint: 'claude openai gemini codex opencode copilot' },
            { section: 'general', sectionLabel: 'General', label: 'Provider', hint: 'anthropic ollama openai-compatible' },
            { section: 'general', sectionLabel: 'General', label: 'Model Override', hint: 'claude sdk model' },
            { section: 'general', sectionLabel: 'General', label: 'Max Tool Turns', hint: 'turns loops' },
            { section: 'general', sectionLabel: 'General', label: 'Bypass Permissions', hint: 'dangerous approve' },
            { section: 'apikeys', sectionLabel: 'API Keys', label: 'Anthropic API Key', hint: 'claude key' },
            { section: 'apikeys', sectionLabel: 'API Keys', label: 'OpenAI API Key', hint: 'gpt key' },
            { section: 'apikeys', sectionLabel: 'API Keys', label: 'Google API Key', hint: 'gemini key' },
            { section: 'apikeys', sectionLabel: 'API Keys', label: 'Tavily API Key', hint: 'search' },
            { section: 'apikeys', sectionLabel: 'API Keys', label: 'ElevenLabs API Key', hint: 'voice tts' },
            { section: 'behavior', sectionLabel: 'Behavior & Safety', label: 'Tool Profile', hint: 'minimal coding full permissions' },
            { section: 'behavior', sectionLabel: 'Behavior & Safety', label: 'Plan Mode', hint: 'approval planning' },
            { section: 'behavior', sectionLabel: 'Behavior & Safety', label: 'Injection Scanner', hint: 'security prompt injection' },
            { section: 'behavior', sectionLabel: 'Behavior & Safety', label: 'Smart Routing', hint: 'model router simple complex' },
            { section: 'memory', sectionLabel: 'Memory', label: 'Memory Backend', hint: 'file mem0' },
            { section: 'memory', sectionLabel: 'Memory', label: 'Auto-Learn', hint: 'mem0 learn' },
            { section: 'memory', sectionLabel: 'Memory', label: 'Embedding Provider', hint: 'mem0 embedder openai ollama' },
            { section: 'memory', sectionLabel: 'Memory', label: 'Vector Store', hint: 'qdrant mem0' },
            { section: 'services', sectionLabel: 'Search & Services', label: 'Web Search Provider', hint: 'tavily brave' },
            { section: 'services', sectionLabel: 'Search & Services', label: 'TTS Provider', hint: 'voice openai elevenlabs sarvam' },
            { section: 'services', sectionLabel: 'Search & Services', label: 'OCR Provider', hint: 'vision tesseract' },
            { section: 'system', sectionLabel: 'System', label: 'Self-Audit Daemon', hint: 'audit schedule' },
        ],

        searchSettings() {
            const q = this.settingsSearch.trim().toLowerCase();
            if (!q) {
                this.settingsSearchResults = [];
                return;
            }
            this.settingsSearchResults = this._SETTINGS_INDEX.filter(item =>
                item.label.toLowerCase().includes(q) ||
                item.hint.toLowerCase().includes(q) ||
                item.sectionLabel.toLowerCase().includes(q)
            );
            // Auto-navigate if all results are in one section
            const sections = [...new Set(this.settingsSearchResults.map(r => r.section))];
            if (sections.length === 1) {
                this.settingsSection = sections[0];
            }
        },

        jumpToSetting(sectionId) {
            this.settingsSection = sectionId;
            this.settingsSearch = '';
            this.settingsSearchResults = [];
            this.settingsMobileView = 'content';
            this.$nextTick(() => { if (window.refreshIcons) window.refreshIcons(); });
        },

        /**
         * Get current time string
         */
        currentTime() {
            return Tools.formatTime();
        },

        /**
         * Dismiss the welcome wizard and set localStorage flag.
         */
        dismissWelcome() {
            this.showWelcome = false;
            localStorage.setItem('pocketpaw_setup_dismissed', '1');
        },

        /**
         * Save an API key during wizard flow, mapping backend to the correct provider.
         */
        _wizardSaveKey(backend, keyValue) {
            const backendToProvider = {
                'claude_agent_sdk': 'anthropic',
                'openai_agents': 'openai',
                'google_adk': 'google',
                'codex_cli': 'openai',
            };
            const provider = backendToProvider[backend];
            if (!provider || !keyValue) return;
            this.apiKeys[provider] = keyValue;
            this.saveApiKey(provider);
        },

        /**
         * Show toast notification
         */
        showToast(message, type = 'info') {
            Tools.showToast(message, type, this.$refs.toasts);
        },

        // Spread feature methods
        ...featureMethods
    };
}

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
            llmProvider: 'auto',
            ollamaHost: 'http://localhost:11434',
            ollamaModel: 'llama3.2',
            anthropicModel: 'claude-sonnet-4-5-20250929',
            openaiCompatibleBaseUrl: '',
            openaiCompatibleApiKey: '',
            openaiCompatibleModel: '',
            openaiCompatibleMaxTokens: 0,
            geminiModel: 'gemini-2.5-flash',
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
            modelTierModerate: 'claude-sonnet-4-5-20250929',
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
            mem0OllamaBaseUrl: 'http://localhost:11434'
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
                // Escape: Cancel rename
                if (e.key === 'Escape' && this.editingSessionId) {
                    this.cancelRenameSession();
                }
            });

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
                'agentBackend', 'llmProvider', 'ollamaHost', 'ollamaModel', 'anthropicModel',
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
                'mem0VectorStore', 'mem0OllamaBaseUrl'
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

            // First-run welcome: show if no API key and not previously dismissed
            if (!this.hasAnthropicKey && !localStorage.getItem('pocketpaw_setup_dismissed')) {
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
            this.showSettings = true;
        },

        /**
         * Save settings
         */
        saveSettings() {
            socket.saveSettings(this.settings);
            this.log('Settings updated', 'info');
            this.showToast('Settings saved', 'success');
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
                'pocketpaw_native': '🐾 PocketPaw',
                'open_interpreter': '🤖 Open Interpreter (Experimental)'
            };
            return labels[this.settings.agentBackend] || this.settings.agentBackend;
        },

        /**
         * Get description for each backend (shown in settings)
         */
        getBackendDescription(backend) {
            const descriptions = {
                'claude_agent_sdk': 'Built-in tools: Bash, WebSearch, WebFetch, Read, Write, Edit, Glob, Grep. Works with Anthropic & Ollama.',
                'pocketpaw_native': 'Custom orchestrator with shell, files, memory tools. Works with Anthropic & Ollama.',
                'open_interpreter': 'Experimental — Standalone agent. Works with local LLMs (Ollama) or cloud APIs.'
            };
            return descriptions[backend] || '';
        },

        /**
         * Get current time string
         */
        currentTime() {
            return Tools.formatTime();
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

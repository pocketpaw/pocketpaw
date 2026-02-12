/**
 * PocketPaw Main Application
 * Alpine.js component for the dashboard
 *
 * Changes (2026-02-05):
 * - MAJOR REFACTOR: Componentized into feature modules using mixin pattern
 * - Extracted features to js/features/: chat, file-browser, reminders, intentions,
 *   skills, transparency, remote-access, mission-control
 * - This file now serves as the core assembler for feature modules
 * - Core functionality: init, WebSocket setup, settings, status, tools, logging
 *
 * Previous changes preserved in feature module files.
 */

function app() {
    // Assemble feature states
    const featureStates = {
        ...window.PocketPaw.Chat.getState(),
        ...window.PocketPaw.FileBrowser.getState(),
        ...window.PocketPaw.Reminders.getState(),
        ...window.PocketPaw.Intentions.getState(),
        ...window.PocketPaw.Skills.getState(),
        ...window.PocketPaw.Transparency.getState(),
        ...window.PocketPaw.RemoteAccess.getState(),
        ...window.PocketPaw.MissionControl.getState(),
        ...window.PocketPaw.Channels.getState(),
        ...window.PocketPaw.KeyboardShortcuts.getState()
    };

    // Assemble feature methods
    const featureMethods = {
        ...window.PocketPaw.Chat.getMethods(),
        ...window.PocketPaw.FileBrowser.getMethods(),
        ...window.PocketPaw.Reminders.getMethods(),
        ...window.PocketPaw.Intentions.getMethods(),
        ...window.PocketPaw.Skills.getMethods(),
        ...window.PocketPaw.Transparency.getMethods(),
        ...window.PocketPaw.RemoteAccess.getMethods(),
        ...window.PocketPaw.MissionControl.getMethods(),
        ...window.PocketPaw.Channels.getMethods(),
        ...window.PocketPaw.KeyboardShortcuts.getMethods()
    };

    return {
        // ==================== Core State ====================

        // View state
        view: 'chat',
        showSettings: false,
        showScreenshot: false,
        screenshotSrc: '',

        // Terminal logs
        logs: [],

        // System status
        status: {
            cpu: '—',
            ram: '—',
            disk: '—',
            battery: '—'
        },

        // Settings
        settings: {
            agentBackend: 'claude_agent_sdk',  // Default: Claude Agent SDK (recommended)
            llmProvider: 'auto',
            anthropicModel: 'claude-sonnet-4-5-20250929',
            bypassPermissions: false
        },

        // API Keys (not persisted client-side, but we track if saved on server)
        apiKeys: {
            anthropic: '',
            openai: ''
        },
        hasAnthropicKey: false,
        hasOpenaiKey: false,

        // Spread feature states
        ...featureStates,

        // ==================== Core Methods ====================

        /**
         * Initialize the app
         */
        init() {
            this.log('PocketPaw Dashboard initialized', 'info');

            // Handle Auth Token (URL capture)
            const urlParams = new URLSearchParams(window.location.search);
            const token = urlParams.get('token');
            if (token) {
                localStorage.setItem('pocketpaw_token', token);
                // Clean URL
                window.history.replaceState({}, document.title, window.location.pathname);
                this.log('Auth token captured and stored', 'success');
            }

            // --- OVERRIDE FETCH FOR AUTH ---
            const originalFetch = window.fetch;
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
                    this.showToast('Session expired. Please re-authenticate.', 'error');
                    // Optionally redirect to login page (if we had one)
                }

                return response;
            };

            // Register event handlers first
            this.setupSocketHandlers();

            // Setup keyboard shortcuts
            this.setupKeyboardShortcuts();

            // Connect WebSocket (singleton - will only connect once)
            socket.connect();

            // Start status polling (low frequency)
            this.startStatusPolling();

            // Refresh Lucide icons after initial render
            this.$nextTick(() => {
                if (window.refreshIcons) window.refreshIcons();
            });
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
            if (data.content) {
                const serverSettings = data.content;
                // Apply server settings to frontend state
                if (serverSettings.agentBackend) {
                    this.settings.agentBackend = serverSettings.agentBackend;
                }
                if (serverSettings.llmProvider) {
                    this.settings.llmProvider = serverSettings.llmProvider;
                }
                if (serverSettings.anthropicModel) {
                    this.settings.anthropicModel = serverSettings.anthropicModel;
                }
                if (serverSettings.bypassPermissions !== undefined) {
                    this.settings.bypassPermissions = serverSettings.bypassPermissions;
                }
                // Store API key availability (for UI feedback)
                this.hasAnthropicKey = serverSettings.hasAnthropicKey || false;
                this.hasOpenaiKey = serverSettings.hasOpenaiKey || false;

                // Log agent status if available (for debugging)
                if (serverSettings.agentStatus) {
                    const status = serverSettings.agentStatus;
                    this.log(`Agent: ${status.backend} (available: ${status.available})`, 'info');
                    if (status.features && status.features.length > 0) {
                        this.log(`Features: ${status.features.join(', ')}`, 'info');
                    }
                }
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
         * Save settings
         */
        saveSettings() {
            socket.saveSettings(
                this.settings.agentBackend,
                this.settings.llmProvider,
                this.settings.anthropicModel,
                this.settings.bypassPermissions
            );
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
            this.log(`Saved ${provider} API key`, 'success');
            this.showToast(`${provider.charAt(0).toUpperCase() + provider.slice(1)} API key saved!`, 'success');
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
                'open_interpreter': '🤖 Open Interpreter'
            };
            return labels[this.settings.agentBackend] || this.settings.agentBackend;
        },

        /**
         * Get description for each backend (shown in settings)
         */
        getBackendDescription(backend) {
            const descriptions = {
                'claude_agent_sdk': 'Built-in tools: Bash, WebSearch, WebFetch, Read, Write, Edit, Glob, Grep',
                'pocketpaw_native': 'Anthropic API + Open Interpreter executor. Direct subprocess for speed.',
                'open_interpreter': 'Standalone agent. Works with local LLMs (Ollama) or cloud APIs.'
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

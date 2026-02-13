/**
 * PocketPaw - Chat Feature Module
 *
 * Created: 2026-02-05
 * Extracted from app.js as part of componentization refactor.
 *
 * Contains chat/messaging functionality:
 * - Message handling
 * - Streaming support
 * - Chat scroll management
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.Chat = {
    name: 'Chat',
    /**
     * Get initial state for Chat
     */
    getState() {
        return {
            // Agent state
            agentActive: true,
            isStreaming: false,
            isThinking: false,
            streamingContent: '',
            streamingMessageId: null,
            hasShownWelcome: false,

            // Messages
            messages: [],
            inputText: ''
        };
    },

    /**
     * Get methods for Chat
     */
    getMethods() {
        return {
            /**
             * Handle notification
             */
            handleNotification(data) {
                const content = data.content || '';

                // Skip duplicate connection messages
                if (content.includes('Connected to PocketPaw') && this.hasShownWelcome) {
                    return;
                }
                if (content.includes('Connected to PocketPaw')) {
                    this.hasShownWelcome = true;
                }

                this.showToast(content, 'info');
                this.log(content, 'info');
            },

            /**
             * Handle incoming message
             */
            handleMessage(data) {
                const content = data.content || '';

                // Check if it's a status update (don't show in chat)
                if (content.includes('System Status') || content.includes('🧠 CPU:')) {
                    this.status = Tools.parseStatus(content);
                    return;
                }

                // Server-side stream flag — auto-enter streaming if we missed stream_start
                if (data.is_stream_chunk && !this.isStreaming) {
                    this.startStreaming();
                }

                // Clear thinking state on first text content
                if (this.isThinking && content) {
                    this.isThinking = false;
                }

                // Handle streaming vs complete messages
                if (this.isStreaming) {
                    this.streamingContent += content;
                    // Scroll during streaming to follow new content
                    this.$nextTick(() => this.scrollToBottom());
                    // Don't log streaming chunks - they flood the terminal
                } else {
                    this.addMessage('assistant', content);
                    // Only log complete messages (not streaming chunks)
                    if (content.trim()) {
                        this.log(content.substring(0, 100) + (content.length > 100 ? '...' : ''), 'info');
                    }
                }
            },

            /**
             * Handle code blocks
             */
            handleCode(data) {
                const content = data.content || '';
                if (this.isStreaming) {
                    this.streamingContent += '\n```\n' + content + '\n```\n';
                } else {
                    this.addMessage('assistant', '```\n' + content + '\n```');
                }
            },

            /**
             * Start streaming mode
             */
            startStreaming() {
                if (this._streamTimeout) {
                    clearTimeout(this._streamTimeout);
                }
                this.isStreaming = true;
                this.isThinking = true;
                this.streamingContent = '';
                // Safety timeout — prevent infinite spinner if backend hangs
                this._streamTimeout = setTimeout(() => {
                    if (this.isStreaming) {
                        this.addMessage('assistant', 'Response timed out. The agent may not be configured — check Settings.');
                        this.endStreaming();
                    }
                }, 90000);
            },

            /**
             * End streaming mode
             */
            endStreaming() {
                if (this._streamTimeout) {
                    clearTimeout(this._streamTimeout);
                    this._streamTimeout = null;
                }
                if (this.isStreaming && this.streamingContent) {
                    this.addMessage('assistant', this.streamingContent);
                }
                this.isStreaming = false;
                this.isThinking = false;
                this.streamingContent = '';

                // Refresh sidebar sessions and auto-title
                if (this.loadSessions) this.loadSessions();
                if (this.autoTitleCurrentSession) this.autoTitleCurrentSession();
            },

            /**
             * Add a message to the chat
             */
            addMessage(role, content) {
                this.messages.push({
                    role,
                    content: content || '',
                    time: Tools.formatTime(),
                    isNew: true
                });

                // Auto scroll to bottom with slight delay for DOM update
                this.$nextTick(() => {
                    this.scrollToBottom();
                });
            },

            /**
             * Scroll chat to bottom
             */
            scrollToBottom() {
                if (this._scrollRAF) return;
                this._scrollRAF = requestAnimationFrame(() => {
                    const el = this.$refs.messages;
                    if (el) el.scrollTop = el.scrollHeight;
                    this._scrollRAF = null;
                });
            },

            /**
             * Send a chat message
             */
            sendMessage() {
                const text = this.inputText.trim();
                if (!text) return;

                // Check for skill command (starts with /)
                if (text.startsWith('/')) {
                    const parts = text.slice(1).split(' ');
                    const skillName = parts[0];
                    const args = parts.slice(1).join(' ');

                    // Add user message
                    this.addMessage('user', text);
                    this.inputText = '';

                    // Run the skill
                    socket.send('run_skill', { name: skillName, args });
                    this.log(`Running skill: /${skillName} ${args}`, 'info');
                    return;
                }

                // Add user message
                this.addMessage('user', text);
                this.inputText = '';

                // Start streaming indicator
                this.startStreaming();

                // Send to server
                socket.chat(text);

                this.log(`You: ${text}`, 'info');
            },

            /**
             * Toggle agent mode
             */
            toggleAgent() {
                socket.toggleAgent(this.agentActive);
                this.log(`Switched Agent Mode: ${this.agentActive ? 'ON' : 'OFF'}`, 'info');
            }
        };
    }
};

window.PocketPaw.Loader.register('Chat', window.PocketPaw.Chat);

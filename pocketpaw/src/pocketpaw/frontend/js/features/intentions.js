/**
 * PocketPaw - Intentions Feature Module
 *
 * Created: 2026-02-05
 * Extracted from app.js as part of componentization refactor.
 *
 * Contains scheduled intentions (cron-based tasks) state and methods:
 * - Intention CRUD operations
 * - Schedule management
 * - Intention execution handling
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.Intentions = {
    name: 'Intentions',
    /**
     * Get initial state for Intentions
     */
    getState() {
        return {
            showIntentions: false,
            intentions: [],
            intentionLoading: false,
            intentionForm: {
                name: '',
                prompt: '',
                schedulePreset: '',
                customCron: '',
                includeSystemStatus: false
            }
        };
    },

    /**
     * Get methods for Intentions
     */
    getMethods() {
        return {
            /**
             * Handle intentions list
             */
            handleIntentions(data) {
                this.intentions = data.intentions || [];
                this.intentionLoading = false;
            },

            /**
             * Handle intention created
             */
            handleIntentionCreated(data) {
                this.intentions.push(data.intention);
                this.resetIntentionForm();
                this.showToast('Intention created!', 'success');
                this.$nextTick(() => {
                    if (window.refreshIcons) window.refreshIcons();
                });
            },

            /**
             * Handle intention updated
             */
            handleIntentionUpdated(data) {
                const index = this.intentions.findIndex(i => i.id === data.intention.id);
                if (index !== -1) {
                    this.intentions[index] = data.intention;
                }
            },

            /**
             * Handle intention toggled
             */
            handleIntentionToggled(data) {
                const index = this.intentions.findIndex(i => i.id === data.intention.id);
                if (index !== -1) {
                    this.intentions[index] = data.intention;
                }
                this.$nextTick(() => {
                    if (window.refreshIcons) window.refreshIcons();
                });
            },

            /**
             * Handle intention deleted
             */
            handleIntentionDeleted(data) {
                this.intentions = this.intentions.filter(i => i.id !== data.id);
            },

            /**
             * Handle intention execution events
             */
            handleIntentionEvent(data) {
                const eventType = data.type;

                if (eventType === 'intention_started') {
                    this.showToast(`Running: ${data.intention_name}`, 'info');
                    this.log(`Intention started: ${data.intention_name}`, 'info');
                    this.startStreaming();
                } else if (eventType === 'intention_completed') {
                    this.log(`Intention completed: ${data.intention_name}`, 'success');
                    this.endStreaming();
                    // Refresh intentions to update next_run time
                    socket.send('get_intentions');
                } else if (eventType === 'intention_error') {
                    this.showToast(`Error: ${data.error}`, 'error');
                    this.log(`Intention error: ${data.error}`, 'error');
                    this.endStreaming();
                } else if (data.content) {
                    // Stream content from agent
                    if (this.isStreaming) {
                        this.streamingContent += data.content;
                    }
                }
            },

            /**
             * Open intentions panel
             */
            openIntentions() {
                this.showIntentions = true;
                this.intentionLoading = true;
                socket.send('get_intentions');

                this.$nextTick(() => {
                    if (window.refreshIcons) window.refreshIcons();
                });
            },

            /**
             * Create a new intention
             */
            createIntention() {
                const { name, prompt, schedulePreset, customCron, includeSystemStatus } = this.intentionForm;

                if (!name.trim() || !prompt.trim() || !schedulePreset) {
                    this.showToast('Please fill in all fields', 'error');
                    return;
                }

                const schedule = schedulePreset === 'custom' ? customCron : schedulePreset;
                if (!schedule) {
                    this.showToast('Please enter a schedule', 'error');
                    return;
                }

                const contextSources = includeSystemStatus ? ['system_status', 'datetime'] : ['datetime'];

                socket.send('create_intention', {
                    name: name.trim(),
                    prompt: prompt.trim(),
                    trigger: { type: 'cron', schedule },
                    context_sources: contextSources,
                    enabled: true
                });

                this.log(`Creating intention: ${name}`, 'info');
            },

            /**
             * Toggle intention enabled state
             */
            toggleIntention(id) {
                socket.send('toggle_intention', { id });
            },

            /**
             * Delete an intention
             */
            deleteIntention(id) {
                if (confirm('Delete this intention?')) {
                    socket.send('delete_intention', { id });
                }
            },

            /**
             * Run an intention immediately
             */
            runIntention(id) {
                socket.send('run_intention', { id });
            },

            /**
             * Reset intention form
             */
            resetIntentionForm() {
                this.intentionForm = {
                    name: '',
                    prompt: '',
                    schedulePreset: '',
                    customCron: '',
                    includeSystemStatus: false
                };
            },

            /**
             * Format next run time for display
             */
            formatNextRun(isoString) {
                if (!isoString) return '';
                const date = new Date(isoString);
                const now = new Date();
                const diff = date - now;

                // If less than 1 hour away, show relative time
                if (diff > 0 && diff < 3600000) {
                    const mins = Math.round(diff / 60000);
                    return `in ${mins}m`;
                }

                // Otherwise show time
                return date.toLocaleString(undefined, {
                    hour: '2-digit',
                    minute: '2-digit'
                });
            }
        };
    }
};

window.PocketPaw.Loader.register('Intentions', window.PocketPaw.Intentions);

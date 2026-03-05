/**
 * PocketPaw - Reminders Feature Module
 *
 * Created: 2026-02-05
 * Extracted from app.js as part of componentization refactor.
 *
 * Contains reminder-related state and methods:
 * - Reminder CRUD operations
 * - Reminder panel management
 * - Time formatting
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.Reminders = {
    name: 'Reminders',
    /**
     * Get initial state for Reminders
     */
    getState() {
        return {
            showReminders: false,
            reminders: [],
            reminderInput: '',
            reminderLoading: false,
            _reminderCountdownTimer: null,
            countdownTick: 0
        };
    },

    /**
     * Get methods for Reminders
     */
    getMethods() {
        return {
            /**
             * Handle reminders list
             */
            handleReminders(data) {
                this.reminders = data.reminders || [];
                this.reminderLoading = false;
            },

            /**
             * Handle reminder added
             */
            handleReminderAdded(data) {
                this.reminders.push(data.reminder);
                this.reminderInput = '';
                this.reminderLoading = false;
                this.showToast('Reminder set!', 'success');
            },

            /**
             * Handle reminder deleted
             */
            handleReminderDeleted(data) {
                this.reminders = this.reminders.filter(r => r.id !== data.id);
            },

            /**
             * Handle reminder triggered (notification)
             */
            handleReminderTriggered(data) {
                const reminder = data.reminder;
                this.showToast(`Reminder: ${reminder.text}`, 'info');
                this.addMessage('assistant', `Reminder: ${reminder.text}`);

                // Remove from local list
                this.reminders = this.reminders.filter(r => r.id !== reminder.id);

                // Try desktop notification
                if (Notification.permission === 'granted') {
                    new Notification('PocketPaw Reminder', {
                        body: reminder.text,
                        icon: '/static/icon.png'
                    });
                }
            },

            /**
             * Open reminders panel
             */
            openReminders() {
                this.showReminders = true;
                this.reminderLoading = true;
                socket.send('get_reminders');

                // Request notification permission
                if (Notification.permission === 'default') {
                    Notification.requestPermission();
                }

                // Start live countdown updates
                this._startReminderCountdown();

                this.$nextTick(() => {
                    if (window.refreshIcons) window.refreshIcons();
                });
            },

            /**
             * Start live countdown timer
             */
            _startReminderCountdown() {
                this._stopReminderCountdown();
                this._reminderCountdownTimer = setInterval(() => {
                    this.countdownTick++;
                }, 1000);
            },

            /**
             * Stop live countdown timer
             */
            _stopReminderCountdown() {
                if (this._reminderCountdownTimer) {
                    clearInterval(this._reminderCountdownTimer);
                    this._reminderCountdownTimer = null;
                }
            },

            /**
             * Close reminders panel and clean up interval
             */
            closeReminders() {
                this._stopReminderCountdown();
                this.showReminders = false;
            },

            /**
             * Calculate time remaining for a reminder (live countdown).
             * Reads countdownTick to establish an Alpine.js reactive dependency
             * so this expression re-evaluates every second.
             */
            calculateTimeRemaining(reminder) {
                const _tick = this.countdownTick; void _tick;

                const now = new Date();
                const triggerTime = new Date(reminder.trigger_at);
                const diff = triggerTime - now;

                if (diff <= 0) {
                    return 'past';
                }

                const totalSeconds = Math.floor(diff / 1000);
                if (totalSeconds < 60) {
                    return `in ${totalSeconds}s`;
                }

                const minutes = Math.floor(totalSeconds / 60);
                if (minutes < 60) {
                    return `in ${minutes}m`;
                }

                const hours = Math.floor(totalSeconds / 3600);
                if (hours < 24) {
                    const remMinutes = Math.floor((totalSeconds % 3600) / 60);
                    if (remMinutes) return `in ${hours}h ${remMinutes}m`;
                    return `in ${hours}h`;
                }

                const days = Math.floor(totalSeconds / 86400);
                return `in ${days}d`;
            },

            /**
             * Add a reminder
             */
            addReminder() {
                const text = this.reminderInput.trim();
                if (!text) return;

                this.reminderLoading = true;
                socket.send('add_reminder', { message: text });
                this.log(`Setting reminder: ${text}`, 'info');
            },

            /**
             * Delete a reminder
             */
            deleteReminder(id) {
                socket.send('delete_reminder', { id });
            },

            /**
             * Format reminder time for display
             */
            formatReminderTime(reminder) {
                const date = new Date(reminder.trigger_at);
                return date.toLocaleString(undefined, {
                    month: 'short',
                    day: 'numeric',
                    hour: '2-digit',
                    minute: '2-digit'
                });
            }
        };
    }
};

window.PocketPaw.Loader.register('Reminders', window.PocketPaw.Reminders);

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
            reminderLoading: false
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

                this.$nextTick(() => {
                    if (window.refreshIcons) window.refreshIcons();
                });
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

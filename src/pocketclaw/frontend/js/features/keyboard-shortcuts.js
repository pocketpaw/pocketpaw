/**
 * PocketPaw - Keyboard Shortcuts Feature Module
 *
 * Created: 2026-02-13
 * Issue #39 — Keyboard shortcut system for the dashboard.
 *
 * Provides:
 * - Global keyboard shortcut dispatcher
 * - Input-aware guard (skips shortcuts while typing, except Escape)
 * - Help overlay toggle (Ctrl/Cmd + /)
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.KeyboardShortcuts = {
    name: 'KeyboardShortcuts',

    getState() {
        return {
            showShortcutsHelp: false,
            /** True when running on macOS (used to display ⌘ vs Ctrl) */
            isMac: /Mac|iPod|iPhone|iPad/.test(navigator.platform || '')
        };
    },

    getMethods() {
        return {
            /**
             * Initialize the keyboard shortcut system.
             * Call this from init() in app.js.
             */
            initKeyboardShortcuts() {
                document.addEventListener('keydown', (e) => {
                    this._handleShortcut(e);
                });
            },

            /**
             * Main shortcut dispatcher.
             * @param {KeyboardEvent} e
             */
            _handleShortcut(e) {
                const mod = e.metaKey || e.ctrlKey;
                const tag = (document.activeElement?.tagName || '').toLowerCase();
                const editable = document.activeElement?.isContentEditable;
                const isTyping = tag === 'input' || tag === 'textarea' || editable;

                // ── Escape — always works, even while typing ──
                if (e.key === 'Escape') {
                    // Priority order: close the most specific overlay first
                    if (this.showShortcutsHelp) {
                        this.showShortcutsHelp = false;
                    } else if (this.showSettings) {
                        this.showSettings = false;
                    } else if (this.showScreenshot) {
                        this.showScreenshot = false;
                    } else if (this.showWelcome) {
                        this.showWelcome = false;
                    } else if (this.showFileBrowser) {
                        this.showFileBrowser = false;
                    } else if (this.editingSessionId) {
                        this.cancelRenameSession();
                    } else if (this.sidebarOpen) {
                        this.sidebarOpen = false;
                    } else {
                        // Blur active element as final fallback
                        document.activeElement?.blur();
                    }
                    return;
                }

                // ── Skip all other shortcuts while user is typing ──
                if (isTyping) return;

                // ── Ctrl/Cmd + K — Focus chat input ──
                if (mod && e.key === 'k') {
                    e.preventDefault();
                    // Switch to chat view first, then focus
                    if (this.navigateToView) {
                        this.navigateToView('chat');
                    } else {
                        this.view = 'chat';
                    }
                    this.$nextTick(() => {
                        const input = document.querySelector(
                            'input[aria-label="Chat message input"]'
                        );
                        if (input) input.focus();
                    });
                    return;
                }

                // ── Ctrl/Cmd + N — New conversation ──
                if (mod && e.key === 'n') {
                    e.preventDefault();
                    this.createNewChat();
                    return;
                }

                // ── Ctrl/Cmd + , — Open settings ──
                if (mod && e.key === ',') {
                    e.preventDefault();
                    this.openSettings();
                    return;
                }

                // ── Ctrl/Cmd + / — Toggle shortcut help ──
                if (mod && e.key === '/') {
                    e.preventDefault();
                    this.showShortcutsHelp = !this.showShortcutsHelp;
                    return;
                }

                // ── Ctrl/Cmd + Shift + A — Toggle activity panel ──
                if (mod && e.shiftKey && (e.key === 'A' || e.key === 'a')) {
                    e.preventDefault();
                    if (this.navigateToView) {
                        this.navigateToView(
                            this.view === 'activity' ? 'chat' : 'activity'
                        );
                    } else {
                        this.view = this.view === 'activity' ? 'chat' : 'activity';
                    }
                    return;
                }
            },

            /**
             * Return the modifier key label for the current OS.
             * @returns {string} '⌘' on macOS, 'Ctrl' elsewhere
             */
            modKey() {
                return this.isMac ? '⌘' : 'Ctrl';
            }
        };
    }
};

window.PocketPaw.Loader.register(
    'KeyboardShortcuts',
    window.PocketPaw.KeyboardShortcuts
);

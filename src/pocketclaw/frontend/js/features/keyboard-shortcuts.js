window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.KeyboardShortcuts = {
    getState() {
        return {
            showKeyboardHelp: false
        };
    },

    getMethods() {
        return {
            setupKeyboardShortcuts() {
                window.addEventListener('keydown', (event) => this.handleKeyboardShortcut(event));
            },

            handleKeyboardShortcut(event) {
                const isMac = navigator.platform.toUpperCase().indexOf('MAC') >= 0;
                const isModifierPressed = isMac ? event.metaKey : event.ctrlKey;
                const isTyping = this.isTypingContext();

                if (event.key === 'Escape') {
                    event.preventDefault();
                    this.closeAnyModal();
                    return;
                }

                if (isTyping && event.key !== 'Escape') {
                    return;
                }

                if (isModifierPressed && event.key === 'k') {
                    event.preventDefault();
                    this.focusChatInput();
                } else if (isModifierPressed && event.key === 'n') {
                    event.preventDefault();
                    this.newConversation();
                } else if (isModifierPressed && event.key === ',') {
                    event.preventDefault();
                    this.showSettings = true;
                } else if (isModifierPressed && event.key === '/') {
                    event.preventDefault();
                    this.toggleKeyboardHelp();
                } else if (isModifierPressed && event.shiftKey && event.key === 'A') {
                    event.preventDefault();
                    this.view = 'activity';
                }
            },

            isTypingContext() {
                const activeElement = document.activeElement;
                return activeElement.tagName === 'INPUT' || 
                       activeElement.tagName === 'TEXTAREA' ||
                       activeElement.isContentEditable;
            },

            getModifierKey() {
                const isMac = navigator.platform.toUpperCase().indexOf('MAC') >= 0;
                return isMac ? '⌘' : 'Ctrl';
            },

            toggleKeyboardHelp() {
                this.showKeyboardHelp = !this.showKeyboardHelp;
            },

            focusChatInput() {
                const chatInput = document.getElementById('chatInput');
                if (chatInput) {
                    chatInput.focus();
                    chatInput.scrollIntoView({ behavior: 'smooth', block: 'center' });
                }
            },

            newConversation() {
                this.messages = [];
                this.isStreaming = false;
                this.streamingContent = '';
                this.streamingMessageId = null;
                this.log('Started new conversation', 'info');
                this.showToast('New conversation started', 'success');
            },

            closeAnyModal() {
                this.showSettings = false;
                this.showKeyboardHelp = false;
                this.showFileBrowser = false;
                this.showReminders = false;
                this.showIntentions = false;
                this.showSkills = false;
                this.showIdentity = false;
                this.showMemory = false;
                this.showAudit = false;
                this.showRemote = false;
                this.showChannels = false;
                this.showScreenshot = false;
            }
        };
    }
};

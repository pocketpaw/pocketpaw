/**
 * PocketPaw - File Browser Feature Module
 *
 * Created: 2026-02-05
 * Updated: 2026-02-17 — Replace context-string routing with EventBus.
 * Previous: 2026-02-16 — output_* context routing for Output Files panel.
 *
 * Contains file browser modal functionality:
 * - Directory navigation
 * - File selection
 * - Breadcrumb navigation
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.FileBrowser = {
    name: 'FileBrowser',
    /**
     * Get initial state for File Browser
     */
    getState() {
        return {
            showFileBrowser: false,
            filePath: '~',
            files: [],
            fileLoading: false,
            fileError: null,
            // File viewer
            showFileViewer: false,
            viewerFileName: '',
            viewerFilePath: '',
            viewerFileType: 'unknown', // 'pdf', 'image', 'text', 'markdown', 'unknown', 'error'
            viewerContentUrl: '',
            viewerTextContent: '',
            viewerHighlightedHtml: '', // hljs-processed HTML for code files
            viewerMarkdownHtml: '',    // DOMPurify-sanitised rendered markdown
            viewerLoading: false,
            // Inline editing
            viewerEditMode: false,
            viewerEditContent: '',
            viewerOriginalContent: '',
            viewerShowDiff: false,
        };
    },

    /**
     * Get methods for File Browser
     */
    getMethods() {
        return {
            /**
             * Handle file browser data
             */
            handleFiles(data) {
                // Route sidebar file tree responses via EventBus
                if (data.context && data.context.startsWith('sidebar_')) {
                    PocketPaw.EventBus.emit('sidebar:files', data);
                    return;
                }
                // Route output file responses via EventBus
                if (data.context && data.context.startsWith('output_')) {
                    PocketPaw.EventBus.emit('output:files', data);
                    return;
                }

                this.fileLoading = false;
                this.fileError = null;

                if (data.error) {
                    this.fileError = data.error;
                    return;
                }

                this.filePath = data.path || '~';
                this.files = data.files || [];

                // Refresh Lucide icons after Alpine renders
                this.$nextTick(() => {
                    if (window.refreshIcons) window.refreshIcons();
                });
            },

            /**
             * Open file browser modal
             */
            openFileBrowser() {
                this.showFileBrowser = true;
                this.fileLoading = true;
                this.fileError = null;
                this.files = [];
                this.filePath = '~';

                // Refresh icons after modal renders
                this.$nextTick(() => {
                    if (window.refreshIcons) window.refreshIcons();
                });

                socket.send('browse', { path: '~' });
            },

            /**
             * Navigate to a directory
             */
            navigateTo(path) {
                this.fileLoading = true;
                this.fileError = null;
                socket.send('browse', { path });
            },

            /**
             * Navigate up one directory
             */
            navigateUp() {
                const parts = this.filePath.split('/').filter(s => s);
                parts.pop();
                const newPath = parts.length > 0 ? parts.join('/') : '~';
                this.navigateTo(newPath);
            },

            /**
             * Navigate to a path segment (breadcrumb click)
             */
            navigateToSegment(index) {
                const parts = this.filePath.split('/').filter(s => s);
                const newPath = parts.slice(0, index + 1).join('/');
                this.navigateTo(newPath || '~');
            },

            /**
             * Select a file or folder
             */
            selectFile(item) {
                if (item.isDir) {
                    // Navigate into directory
                    const newPath = this.filePath === '~'
                        ? item.name
                        : `${this.filePath}/${item.name}`;
                    this.navigateTo(newPath);
                } else {
                    const fullPath = this.filePath === '~'
                        ? item.name
                        : `${this.filePath}/${item.name}`;
                    this.openFileViewer(fullPath);
                }
            },

            /**
             * Handle open_path WebSocket event from backend
             */
            handleOpenPath(data) {
                if (data.action === 'navigate') {
                    this.showFileBrowser = true;
                    this.navigateTo(data.path);
                } else if (data.action === 'view') {
                    this.openFileViewer(data.path);
                }
            },

            /**
             * Detect file type from extension
             */
            _detectFileType(filename) {
                const ext = (filename.split('.').pop() || '').toLowerCase();
                if (ext === 'pdf') return 'pdf';
                const imageExts = [
                    'jpg', 'jpeg', 'png', 'gif', 'svg', 'webp', 'bmp', 'ico',
                ];
                if (imageExts.includes(ext)) return 'image';
                const textExts = [
                    'txt', 'md', 'py', 'js', 'ts', 'json', 'html', 'css',
                    'yaml', 'yml', 'toml', 'cfg', 'ini', 'log', 'sh', 'bat',
                    'xml', 'csv', 'env', 'rs', 'go', 'java', 'c', 'cpp', 'h',
                    'jsx', 'tsx', 'svelte', 'vue', 'rb', 'php', 'sql', 'r',
                    'swift', 'kt', 'lua', 'pl', 'dockerfile', 'makefile',
                ];
                if (textExts.includes(ext)) return 'text';
                return 'unknown';
            },

            /**
             * Apply syntax highlighting or markdown rendering to the
             * current viewerTextContent based on the file extension.
             * Shared by openFileViewer() and saveFileEdits().
             */
            _applyHighlighting() {
                const ext = (this.viewerFileName.split('.').pop() || '')
                    .toLowerCase();
                const isMarkdown = ext === 'md' || ext === 'markdown';
                if (isMarkdown) {
                    this.viewerMarkdownHtml = DOMPurify.sanitize(
                        marked.parse(this.viewerTextContent),
                    );
                    this.viewerFileType = 'markdown';
                } else if (window.hljs) {
                    const lang = hljs.getLanguage(ext)
                        ? ext : 'plaintext';
                    this.viewerHighlightedHtml = hljs.highlight(
                        this.viewerTextContent, { language: lang },
                    ).value;
                }
            },

            /**
             * Open file in the in-app viewer modal
             */
            async openFileViewer(filePath) {
                const fileName = filePath.split(/[/\\]/).pop() || filePath;
                const fileType = this._detectFileType(fileName);
                const contentUrl = `/api/v1/files/content?path=${encodeURIComponent(filePath)}`;

                this.viewerFileName = fileName;
                this.viewerFilePath = filePath;
                this.viewerFileType = fileType;
                this.viewerContentUrl = contentUrl;
                this.viewerTextContent = '';
                this.viewerHighlightedHtml = '';
                this.viewerMarkdownHtml = '';
                this.viewerLoading = true;
                this.showFileViewer = true;
                this.viewerEditMode = false;
                this.viewerEditContent = '';
                this.viewerOriginalContent = '';
                this.viewerShowDiff = false;

                if (fileType === 'text') {
                    try {
                        const resp = await fetch(contentUrl);
                        if (!resp.ok) {
                            const err = await resp.json().catch(() => ({ detail: resp.statusText }));
                            this.viewerTextContent = `Error: ${err.detail || resp.statusText}`;
                            this.viewerFileType = 'error';
                        } else {
                            this.viewerTextContent = await resp.text();
                            this._applyHighlighting();
                        }
                    } catch (e) {
                        this.viewerTextContent = `Error loading file: ${e.message}`;
                        this.viewerFileType = 'error';
                    }
                }

                this.viewerLoading = false;
                this.$nextTick(() => { if (window.refreshIcons) window.refreshIcons(); });
            },

            /**
             * Close the file viewer
             */
            closeFileViewer() {
                this.showFileViewer = false;
                this.viewerTextContent = '';
                this.viewerHighlightedHtml = '';
                this.viewerMarkdownHtml = '';
                this.viewerContentUrl = '';
                this.viewerEditMode = false;
                this.viewerEditContent = '';
                this.viewerOriginalContent = '';
                this.viewerShowDiff = false;
            },

            /**
             * Close the viewer and insert a reference to the file into the chat input.
             * Navigates to the chat view if the hash router is active.
             */
            addFileToChat() {
                const path = this.viewerFilePath;
                const prefix = `Please read the file at ${path} and `;
                this.inputText = prefix;
                this.closeFileViewer();
                this.showFileBrowser = false;
                // Navigate to chat view via hash router if available
                if (typeof window.navigateToView === 'function') {
                    window.navigateToView('chat');
                }
                // Focus the chat input on the next tick so the view has rendered
                this.$nextTick(() => {
                    const input = this.$refs.chatInput;
                    if (input) {
                        input.focus();
                        // Move cursor to end
                        input.setSelectionRange(input.value.length, input.value.length);
                    }
                });
            },

            /**
             * Trigger a browser download for a single file
             */
            downloadFile(filePath) {
                const a = document.createElement('a');
                a.href = `/api/v1/files/download?path=${encodeURIComponent(filePath)}`;
                a.download = '';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
            },

            /**
             * Trigger a zip download for a directory
             */
            downloadDirAsZip(dirPath) {
                const a = document.createElement('a');
                a.href = `/api/v1/files/download-zip?path=${encodeURIComponent(dirPath)}`;
                a.download = '';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
            },

            /**
             * Toggle inline editor mode for text files
             */
            toggleEditMode() {
                if (!this.viewerEditMode) {
                    this.viewerEditContent = this.viewerTextContent;
                    this.viewerOriginalContent = this.viewerTextContent;
                    this.viewerShowDiff = false;
                }
                this.viewerEditMode = !this.viewerEditMode;
            },

            /**
             * Save edited file content back to the server
             */
            async saveFileEdits() {
                try {
                    const res = await fetch('/api/v1/files/write', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            path: this.viewerFilePath,
                            content: this.viewerEditContent,
                        }),
                    });
                    if (!res.ok) {
                        const err = await res.json().catch(() => ({}));
                        this.showToast(err.detail || 'Save failed', 'error');
                        return;
                    }
                    this.viewerTextContent = this.viewerEditContent;
                    this.viewerOriginalContent = this.viewerEditContent;
                    // Re-apply highlighting after save
                    this._applyHighlighting();
                    this.viewerEditMode = false;
                    this.viewerShowDiff = false;
                    this.showToast('File saved', 'success');
                } catch (e) {
                    this.showToast('Save failed: ' + e.message, 'error');
                }
            },

            /**
             * Toggle diff view (original vs edited)
             */
            toggleDiffView() {
                this.viewerShowDiff = !this.viewerShowDiff;
            },

            /**
             * Compute a simple line-by-line diff between original and modified text.
             * Returns an array of { type: 'same'|'added'|'removed', line: string, num: number }.
             */
            _computeDiff(original, modified) {
                const origLines = original.split('\n');
                const modLines = modified.split('\n');
                const result = [];
                const maxLen = Math.max(origLines.length, modLines.length);
                for (let i = 0; i < maxLen; i++) {
                    const o = origLines[i];
                    const m = modLines[i];
                    if (o === m) {
                        result.push({ type: 'same', line: m ?? '', num: i + 1 });
                    } else if (o === undefined) {
                        result.push({ type: 'added', line: m, num: i + 1 });
                    } else if (m === undefined) {
                        result.push({ type: 'removed', line: o, num: i + 1 });
                    } else {
                        result.push({ type: 'removed', line: o, num: i + 1 });
                        result.push({ type: 'added', line: m, num: i + 1 });
                    }
                }
                return result;
            }
        };
    }
};

window.PocketPaw.Loader.register('FileBrowser', window.PocketPaw.FileBrowser);

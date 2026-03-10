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
            viewerFileType: 'unknown', // 'pdf', 'image', 'text', 'unknown'
            viewerContentUrl: '',
            viewerTextContent: '',
            viewerLoading: false,
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
                if (['jpg', 'jpeg', 'png', 'gif', 'svg', 'webp', 'bmp', 'ico'].includes(ext)) return 'image';
                if ([
                    'txt', 'md', 'py', 'js', 'ts', 'json', 'html', 'css',
                    'yaml', 'yml', 'toml', 'cfg', 'ini', 'log', 'sh', 'bat',
                    'xml', 'csv', 'env', 'rs', 'go', 'java', 'c', 'cpp', 'h',
                    'jsx', 'tsx', 'svelte', 'vue', 'rb', 'php', 'sql', 'r',
                    'swift', 'kt', 'lua', 'pl', 'dockerfile', 'makefile',
                ].includes(ext)) return 'text';
                return 'unknown';
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
                this.viewerLoading = true;
                this.showFileViewer = true;

                if (fileType === 'text') {
                    try {
                        const resp = await fetch(contentUrl);
                        if (!resp.ok) {
                            const err = await resp.json().catch(() => ({ detail: resp.statusText }));
                            this.viewerTextContent = `Error: ${err.detail || resp.statusText}`;
                            this.viewerFileType = 'error';
                        } else {
                            this.viewerTextContent = await resp.text();
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
                this.viewerContentUrl = '';
            }
        };
    }
};

window.PocketPaw.Loader.register('FileBrowser', window.PocketPaw.FileBrowser);

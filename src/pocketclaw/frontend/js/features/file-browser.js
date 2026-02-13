/**
 * PocketPaw - File Browser Feature Module
 *
 * Created: 2026-02-05
 * Updated: 2026-02-12 — handleFiles routes sidebar_* context responses to
 *   ProjectBrowser.handleSidebarFiles() instead of updating modal state.
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
            fileError: null
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
                // Route sidebar file tree responses to ProjectBrowser
                if (data.context && data.context.startsWith('sidebar_')) {
                    this.handleSidebarFiles(data);
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
                    // File selected - could download or preview
                    this.log(`Selected file: ${item.name}`, 'info');
                    this.showToast(`Selected: ${item.name}`, 'info');
                    // TODO: Add file download/preview functionality
                }
            }
        };
    }
};

window.PocketPaw.Loader.register('FileBrowser', window.PocketPaw.FileBrowser);

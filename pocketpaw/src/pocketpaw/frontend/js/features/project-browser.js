/**
 * PocketPaw - Project Browser Feature Module
 *
 * Rewritten: 2026-02-12
 * Replaced flat project cards with inline file tree in sidebar + project navigation.
 *
 * State:
 *   sidebarTab, sidebarProjects, sidebarProjectsLoading, sidebarProjectSearch,
 *   projectFileTrees — nested state for per-project expandable file trees
 *
 * Methods:
 *   loadSidebarProjects, getFilteredSidebarProjects, switchSidebarTab,
 *   getSidebarStatusDot, toggleProjectTree, loadProjectDir,
 *   handleSidebarFiles, toggleProjectDir, getProjectTreeItems,
 *   isProjectDirExpanded, isProjectDirLoading, openFileInBrowser,
 *   navigateToProject
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.ProjectBrowser = {
    name: 'ProjectBrowser',

    getState() {
        return {
            sidebarTab: 'chats',
            sidebarProjects: [],
            sidebarProjectsLoading: false,
            sidebarProjectSearch: '',
            _sidebarProjectsLoaded: false,
            // Inline file tree state: { [projectId]: { expanded, dirs: { [path]: { files, loading, expanded } } } }
            projectFileTrees: {}
        };
    },

    getMethods() {
        return {
            /**
             * Fetch projects from the Mission Control API for sidebar display.
             */
            async loadSidebarProjects() {
                this.sidebarProjectsLoading = true;
                try {
                    const res = await fetch('/api/mission-control/projects');
                    if (res.ok) {
                        const data = await res.json();
                        this.sidebarProjects = data.projects || [];
                    }
                } catch (e) {
                    console.error('Failed to load sidebar projects:', e);
                } finally {
                    this.sidebarProjectsLoading = false;
                    this._sidebarProjectsLoaded = true;
                    this.$nextTick(() => {
                        if (window.refreshIcons) window.refreshIcons();
                    });
                }
            },

            /**
             * Filter sidebar projects by search term.
             */
            getFilteredSidebarProjects() {
                const q = (this.sidebarProjectSearch || '').toLowerCase().trim();
                if (!q) return this.sidebarProjects;
                return this.sidebarProjects.filter(p =>
                    (p.title || '').toLowerCase().includes(q) ||
                    (p.status || '').toLowerCase().includes(q)
                );
            },

            /**
             * Switch sidebar tab. Lazy-loads projects on first switch.
             */
            switchSidebarTab(tab) {
                this.sidebarTab = tab;
                if (tab === 'projects' && !this._sidebarProjectsLoaded) {
                    this.loadSidebarProjects();
                }
                this.$nextTick(() => {
                    if (window.refreshIcons) window.refreshIcons();
                });
            },

            /**
             * Get Tailwind color class for the project status dot.
             */
            getSidebarStatusDot(status) {
                const dots = {
                    'draft': 'bg-gray-400',
                    'planning': 'bg-blue-400 animate-pulse',
                    'awaiting_approval': 'bg-amber-400',
                    'approved': 'bg-cyan-400',
                    'executing': 'bg-green-400 animate-pulse',
                    'paused': 'bg-orange-400',
                    'completed': 'bg-emerald-400',
                    'failed': 'bg-red-400'
                };
                return dots[status] || 'bg-white/30';
            },

            // ==================== Inline File Tree ====================

            /**
             * Toggle the inline file tree for a project (expand/collapse).
             * Lazy-loads root files on first expand.
             */
            toggleProjectTree(project) {
                const id = project.id;
                const trees = this.projectFileTrees;
                const current = trees[id];

                if (current && current.expanded) {
                    // Collapse
                    this.projectFileTrees = {
                        ...trees,
                        [id]: { ...current, expanded: false }
                    };
                } else if (current) {
                    // Re-expand (files already loaded)
                    this.projectFileTrees = {
                        ...trees,
                        [id]: { ...current, expanded: true }
                    };
                } else {
                    // First expand — init tree and load root
                    // Use folder_path from API (e.g. ~/pocketpaw-projects/<id>)
                    const rootPath = project.folder_path || ('pocketpaw-projects/' + id);
                    this.projectFileTrees = {
                        ...trees,
                        [id]: {
                            expanded: true,
                            rootPath: rootPath,
                            dirs: {
                                [rootPath]: { files: [], loading: true, expanded: true }
                            }
                        }
                    };
                    this.loadProjectDir(id, rootPath);
                }
                this.$nextTick(() => {
                    if (window.refreshIcons) window.refreshIcons();
                });
            },

            /**
             * Load files for a directory within a project's tree.
             * Sends a browse command with context so the response is routed here.
             */
            loadProjectDir(projectId, dirPath) {
                const ctx = 'sidebar_' + projectId + '_' + dirPath;
                socket.send('browse', { path: dirPath, context: ctx });
            },

            /**
             * Handle sidebar file tree responses (routed from handleFiles).
             * Parses the context string to identify project and directory.
             */
            handleSidebarFiles(data) {
                // context format: sidebar_{projectId}_{dirPath}
                const ctx = data.context || '';
                const firstUnderscore = ctx.indexOf('_');
                if (firstUnderscore === -1) return;

                const rest = ctx.substring(firstUnderscore + 1);
                const secondUnderscore = rest.indexOf('_');
                if (secondUnderscore === -1) return;

                const projectId = rest.substring(0, secondUnderscore);
                const dirPath = rest.substring(secondUnderscore + 1);

                const trees = this.projectFileTrees;
                const tree = trees[projectId];
                if (!tree) return;

                const files = data.error ? [] : (data.files || []);
                const updatedDirs = {
                    ...tree.dirs,
                    [dirPath]: {
                        files: files,
                        loading: false,
                        expanded: true
                    }
                };

                this.projectFileTrees = {
                    ...trees,
                    [projectId]: { ...tree, dirs: updatedDirs }
                };

                this.$nextTick(() => {
                    if (window.refreshIcons) window.refreshIcons();
                });
            },

            /**
             * Toggle a subdirectory within the inline tree.
             * Lazy-loads on first expand. Max 2 levels deep from root.
             */
            toggleProjectDir(projectId, dirPath) {
                const trees = this.projectFileTrees;
                const tree = trees[projectId];
                if (!tree) return;

                const dirState = tree.dirs[dirPath];
                if (dirState) {
                    // Toggle existing dir
                    const updatedDirs = {
                        ...tree.dirs,
                        [dirPath]: { ...dirState, expanded: !dirState.expanded }
                    };
                    this.projectFileTrees = {
                        ...trees,
                        [projectId]: { ...tree, dirs: updatedDirs }
                    };
                } else {
                    // First expand — check depth (max 2 levels from root)
                    const rootPath = tree.rootPath || '';
                    const rootDepth = rootPath.split('/').length;
                    const dirDepth = dirPath.split('/').length;

                    if (dirDepth - rootDepth >= 2) {
                        // Too deep — open in file browser modal instead
                        this.openFileInBrowser(projectId, dirPath);
                        return;
                    }

                    // Init and load
                    const updatedDirs = {
                        ...tree.dirs,
                        [dirPath]: { files: [], loading: true, expanded: true }
                    };
                    this.projectFileTrees = {
                        ...trees,
                        [projectId]: { ...tree, dirs: updatedDirs }
                    };
                    this.loadProjectDir(projectId, dirPath);
                }

                this.$nextTick(() => {
                    if (window.refreshIcons) window.refreshIcons();
                });
            },

            /**
             * Get files for a directory in the project tree.
             */
            getProjectTreeItems(projectId, dirPath) {
                const tree = this.projectFileTrees[projectId];
                if (!tree || !tree.dirs[dirPath]) return [];
                return tree.dirs[dirPath].files || [];
            },

            /**
             * Check if a project tree directory is expanded.
             */
            isProjectDirExpanded(projectId, dirPath) {
                const tree = this.projectFileTrees[projectId];
                if (!tree || !tree.dirs[dirPath]) return false;
                return tree.dirs[dirPath].expanded;
            },

            /**
             * Check if a project tree directory is loading.
             */
            isProjectDirLoading(projectId, dirPath) {
                const tree = this.projectFileTrees[projectId];
                if (!tree || !tree.dirs[dirPath]) return false;
                return tree.dirs[dirPath].loading;
            },

            /**
             * Check if a project's root tree is expanded.
             */
            isProjectTreeExpanded(projectId) {
                const tree = this.projectFileTrees[projectId];
                return tree ? tree.expanded : false;
            },

            /**
             * Open the file browser modal at a specific path within a project.
             */
            openFileInBrowser(projectId, filePath) {
                this.showFileBrowser = true;
                this.fileLoading = true;
                this.fileError = null;
                this.files = [];
                this.filePath = filePath;
                this.sidebarOpen = false;
                this.$nextTick(() => {
                    if (window.refreshIcons) window.refreshIcons();
                });
                socket.send('browse', { path: filePath });
            },

            /**
             * Navigate to the Crew view with a specific project selected.
             * Updates the hash route for persistence.
             */
            navigateToProject(project) {
                // Switch to Crew view, Projects tab
                this.view = 'missions';
                this.missionControl.crewTab = 'projects';

                // Load MC data if not loaded
                this.loadMCData();

                // Select the project (loads tasks/PRD/progress)
                this.selectProject(project);

                // Update hash
                if (this.updateHash) {
                    this.updateHash('#/project/' + project.id);
                }

                // Close mobile sidebar
                this.sidebarOpen = false;
            },

            /**
             * Get the file icon name for a file extension.
             */
            getFileIcon(fileName) {
                const ext = (fileName || '').split('.').pop().toLowerCase();
                const icons = {
                    'md': 'file-text',
                    'txt': 'file-text',
                    'json': 'file-json',
                    'py': 'file-code',
                    'js': 'file-code',
                    'ts': 'file-code',
                    'html': 'file-code',
                    'css': 'file-code',
                    'yaml': 'file-cog',
                    'yml': 'file-cog',
                    'toml': 'file-cog',
                    'png': 'image',
                    'jpg': 'image',
                    'svg': 'image',
                    'pdf': 'file-text'
                };
                return icons[ext] || 'file';
            }
        };
    }
};

window.PocketPaw.Loader.register('ProjectBrowser', window.PocketPaw.ProjectBrowser);

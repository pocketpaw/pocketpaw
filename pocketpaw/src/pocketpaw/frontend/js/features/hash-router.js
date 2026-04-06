/**
 * PocketPaw - Hash Router Feature Module
 *
 * Created: 2026-02-12
 * Hash-based URL routing for view state persistence across page refreshes
 * and browser back/forward navigation.
 *
 * Route table:
 *   #/chat           → view = 'chat'
 *   #/activity       → view = 'activity'
 *   #/terminal       → view = 'terminal'
 *   #/crew           → view = 'missions', crewTab = 'tasks'
 *   #/crew/projects  → view = 'missions', crewTab = 'projects'
 *   #/project/{id}   → view = 'missions', crewTab = 'projects', selectProject(id)
 *
 * State:
 *   _hashRouterInitialized, _suppressHashChange
 *
 * Methods:
 *   initHashRouter, navigateToView, updateHash, _parseHash, _applyRoute
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.HashRouter = {
    name: 'HashRouter',

    getState() {
        return {
            _hashRouterInitialized: false,
            _suppressHashChange: false
        };
    },

    getMethods() {
        return {
            /**
             * Initialize hash router — parse initial hash and listen for changes.
             * Called from app.js init() after WebSocket setup.
             */
            initHashRouter() {
                if (this._hashRouterInitialized) return;
                this._hashRouterInitialized = true;

                // Listen for browser back/forward
                window.addEventListener('hashchange', () => {
                    if (this._suppressHashChange) {
                        this._suppressHashChange = false;
                        return;
                    }
                    const route = this._parseHash();
                    this._applyRoute(route);
                });

                // Apply initial hash on page load
                const hash = window.location.hash;
                if (hash && hash.length > 1) {
                    const route = this._parseHash();
                    this._applyRoute(route);
                }
            },

            /**
             * Navigate to a view and update the hash.
             * Replaces direct `view = 'xxx'` assignments in the top bar.
             */
            navigateToView(viewName) {
                this.view = viewName;

                // Load MC data when switching to Crew
                if (viewName === 'missions') {
                    this.loadMCData();
                }

                // Map view names to hash routes
                const hashMap = {
                    'chat': '#/chat',
                    'activity': '#/activity',
                    'terminal': '#/terminal',
                    'missions': '#/crew'
                };
                this.updateHash(hashMap[viewName] || '#/chat');
            },

            /**
             * Update the URL hash without triggering the hashchange handler.
             */
            updateHash(hash) {
                if (window.location.hash === hash) return;
                this._suppressHashChange = true;
                window.location.hash = hash;
            },

            /**
             * Parse the current URL hash into a route object.
             */
            _parseHash() {
                const hash = window.location.hash || '';
                // Strip leading #
                const path = hash.startsWith('#') ? hash.substring(1) : hash;
                // Strip leading /
                const clean = path.startsWith('/') ? path.substring(1) : path;
                const parts = clean.split('/');

                // Default route
                const route = { view: 'chat', crewTab: null, projectId: null };

                if (parts[0] === 'chat') {
                    route.view = 'chat';
                } else if (parts[0] === 'activity') {
                    route.view = 'activity';
                } else if (parts[0] === 'terminal') {
                    route.view = 'terminal';
                } else if (parts[0] === 'crew') {
                    route.view = 'missions';
                    route.crewTab = parts[1] === 'projects' ? 'projects' : 'tasks';
                } else if (parts[0] === 'project' && parts[1]) {
                    route.view = 'missions';
                    route.crewTab = 'projects';
                    route.projectId = parts[1];
                }

                return route;
            },

            /**
             * Apply a parsed route to the Alpine state.
             */
            _applyRoute(route) {
                this.view = route.view;

                if (route.view === 'missions') {
                    this.loadMCData();

                    if (route.crewTab) {
                        this.missionControl.crewTab = route.crewTab;
                    }

                    if (route.crewTab === 'projects') {
                        this.loadProjects();
                    }

                    // Deferred project selection — wait for projects to load
                    if (route.projectId) {
                        this._selectProjectById(route.projectId);
                    }
                }

                this.$nextTick(() => {
                    if (window.refreshIcons) window.refreshIcons();
                });
            },

            /**
             * Select a project by ID, waiting for the project list to load if needed.
             */
            async _selectProjectById(projectId) {
                // Try immediate match
                let project = this.missionControl.projects.find(p => p.id === projectId);
                if (project) {
                    this.selectProject(project);
                    return;
                }

                // Projects may not be loaded yet — wait a bit and retry
                await new Promise(resolve => setTimeout(resolve, 500));

                // Try from sidebar projects (may have loaded separately)
                project = this.sidebarProjects.find(p => p.id === projectId);
                if (!project) {
                    project = this.missionControl.projects.find(p => p.id === projectId);
                }

                if (project) {
                    this.selectProject(project);
                } else {
                    // Last resort: fetch directly
                    try {
                        const res = await fetch(`/api/deep-work/projects/${projectId}/plan`);
                        if (res.ok) {
                            const data = await res.json();
                            if (data.project) {
                                this.selectProject(data.project);
                            }
                        }
                    } catch (e) {
                        console.error('Failed to load project from hash:', e);
                    }
                }
            }
        };
    }
};

window.PocketPaw.Loader.register('HashRouter', window.PocketPaw.HashRouter);

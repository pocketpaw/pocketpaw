/**
 * PocketPaw - Mission Control: Deep Work Module
 *
 * Created: 2026-02-17 — Split from mission-control.js (1,699-line monolith).
 * Updated: 2026-02-18 — Added Goal Parser integration: analyzeGoal(), two-step
 *   start flow (analyze → review → plan), goal analysis state and display helpers.
 *
 * Contains Deep Work project orchestration state and methods:
 * - Goal analysis (analyzeGoal, domain/complexity display)
 * - Project CRUD (load, start, approve, pause, resume, delete)
 * - Project selection and detail loading
 * - Project status helpers (color, label, icon)
 * - Planning phase info
 * - Active project count
 * - Output Files panel (loadProjectOutputFiles, handleOutputFiles, openOutputFile)
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.DeepWork = {
    name: 'DeepWork',

    getState() {
        return {
            missionControl: {
                crewTab: 'tasks',              // 'tasks' | 'projects'
                projects: [],                  // List of projects
                selectedProject: null,         // Currently selected project
                projectTasks: [],              // Tasks for selected project
                projectPrd: null,              // PRD document for selected project
                projectProgress: null,         // {completed, total, percent}
                showStartProject: false,       // Start project modal
                showProjectDetail: false,      // Full project detail sheet
                projectInput: '',              // Natural language project input
                researchDepth: 'auto',         // 'auto' | 'none' | 'quick' | 'standard' | 'deep'
                projectStarting: false,        // Loading state while planner runs
                planningPhase: '',             // Current phase: goal_analysis, research, prd, tasks, team
                planningMessage: '',           // Phase progress message
                planningProjectId: null,       // Project being planned
                // Goal analysis state
                goalAnalysis: null,            // Parsed goal analysis from /parse-goal
                goalAnalyzing: false,          // Loading state while goal parser runs
                goalAnalysisStep: 'input',     // 'input' | 'review' — modal step
                // Output Files panel state
                projectOutputFiles: [],        // files in project output directory
                projectOutputLoading: false,   // loading state for output files
                _outputExpanded: true,         // output panel starts expanded
            }
        };
    },

    getMethods() {
        return {
            // ==================== Deep Work Projects ====================

            /**
             * Load all Deep Work projects
             */
            async loadProjects() {
                try {
                    const res = await fetch('/api/mission-control/projects');
                    if (res.ok) {
                        const data = await res.json();
                        this.missionControl.projects = data.projects || [];
                    }
                } catch (e) {
                    console.error('Failed to load projects:', e);
                }
                this.$nextTick(() => { if (window.refreshIcons) window.refreshIcons(); });
            },

            /**
             * Analyze a goal before starting planning (Step 1 of 2-step flow)
             */
            async analyzeGoal() {
                const input = this.missionControl.projectInput.trim();
                if (!input || input.length < 10) {
                    this.showToast('Please describe your project (at least 10 characters)', 'error');
                    return;
                }

                this.missionControl.goalAnalyzing = true;
                this.missionControl.goalAnalysis = null;

                try {
                    const res = await fetch('/api/deep-work/parse-goal', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ description: input })
                    });

                    if (res.ok) {
                        const data = await res.json();
                        this.missionControl.goalAnalysis = data.goal_analysis;
                        this.missionControl.researchDepth = data.goal_analysis.suggested_research_depth || 'standard';
                        this.missionControl.goalAnalysisStep = 'review';
                    } else {
                        const err = await res.json();
                        this.showToast(err.detail || 'Goal analysis failed', 'error');
                    }
                } catch (e) {
                    console.error('Failed to analyze goal:', e);
                    this.showToast('Goal analysis failed — you can still start planning', 'error');
                } finally {
                    this.missionControl.goalAnalyzing = false;
                    this.$nextTick(() => { if (window.refreshIcons) window.refreshIcons(); });
                }
            },

            /**
             * Reset goal analysis and go back to input step.
             * If soft=true (modal close), cache the analysis so reopening restores it.
             */
            resetGoalAnalysis(soft = false) {
                if (soft && this.missionControl.goalAnalysis) {
                    // Cache: keep analysis so reopening the modal restores the review step
                    return;
                }
                this.missionControl.goalAnalysis = null;
                this.missionControl.goalAnalysisStep = 'input';
                this.missionControl.researchDepth = 'auto';
            },

            /**
             * Start a new Deep Work project from natural language input (Step 2)
             */
            async startDeepWork() {
                const input = this.missionControl.projectInput.trim();
                if (!input || input.length < 10) {
                    this.showToast('Please describe your project (at least 10 characters)', 'error');
                    return;
                }

                this.missionControl.projectStarting = true;
                this.missionControl.planningPhase = 'starting';
                this.missionControl.planningMessage = 'Initializing project...';

                try {
                    const body = {
                        description: input,
                        research_depth: this.missionControl.researchDepth
                    };
                    // Pass pre-parsed goal analysis to skip re-parsing
                    if (this.missionControl.goalAnalysis) {
                        body.goal_analysis = this.missionControl.goalAnalysis;
                    }

                    const res = await fetch('/api/deep-work/start', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(body)
                    });

                    if (res.ok) {
                        const data = await res.json();
                        const project = data.project;
                        this.missionControl.projects.unshift(project);
                        this.missionControl.projectInput = '';
                        this.missionControl.showStartProject = false;
                        this.missionControl.goalAnalysis = null;
                        this.missionControl.goalAnalysisStep = 'input';

                        // Set planningProjectId IMMEDIATELY so WebSocket phase
                        // events can be tracked (planning runs in background)
                        this.missionControl.planningProjectId = project.id;

                        // Auto-select the project (shows planning status)
                        this.missionControl.selectedProject = project;
                        this.missionControl.projectTasks = [];
                        this.missionControl.projectPrd = null;
                        this.missionControl.projectProgress = null;

                        this.showToast('Planning started...', 'info');
                        // Planning completion will be handled by handleDWEvent
                        // when dw_planning_complete arrives via WebSocket
                    } else {
                        const err = await res.json();
                        this.showToast(err.detail || 'Failed to start project', 'error');
                        this.missionControl.projectStarting = false;
                        this.missionControl.planningPhase = '';
                        this.missionControl.planningMessage = '';
                        this.missionControl.planningProjectId = null;
                    }
                } catch (e) {
                    console.error('Failed to start Deep Work:', e);
                    this.showToast('Failed to start project', 'error');
                    this.missionControl.projectStarting = false;
                    this.missionControl.planningPhase = '';
                    this.missionControl.planningMessage = '';
                    this.missionControl.planningProjectId = null;
                }
            },

            /**
             * Select a project and load its details
             */
            async selectProject(project) {
                this.missionControl.selectedProject = project;
                this.missionControl.projectTasks = [];
                if (this._invalidateTaskCache) this._invalidateTaskCache();
                this.missionControl.projectPrd = null;
                this.missionControl.projectProgress = null;
                this.missionControl.executionLevels = [];
                this.missionControl.taskLevelMap = {};
                this.missionControl.expandedTaskId = null;
                this.missionControl.taskDeliverableCache = {};
                this.missionControl.projectOutputFiles = [];
                this.missionControl.projectOutputLoading = false;

                try {
                    const res = await fetch(`/api/deep-work/projects/${project.id}/plan`);
                    if (res.ok) {
                        const data = await res.json();
                        // Update project from server (may be newer)
                        this.missionControl.selectedProject = data.project;
                        this.missionControl.projectTasks = data.tasks || [];
                        if (this._invalidateTaskCache) this._invalidateTaskCache();
                        this.missionControl.projectProgress = data.progress || null;
                        this.missionControl.projectPrd = data.prd || null;
                        this.missionControl.executionLevels = data.execution_levels || [];
                        this.missionControl.taskLevelMap = data.task_level_map || {};

                        // Also update in projects list
                        const idx = this.missionControl.projects.findIndex(p => p.id === project.id);
                        if (idx >= 0) {
                            this.missionControl.projects[idx] = data.project;
                        }

                        // Load output files for the project
                        this.loadProjectOutputFiles();
                    }
                } catch (e) {
                    console.error('Failed to load project detail:', e);
                }
                this.$nextTick(() => { if (window.refreshIcons) window.refreshIcons(); });
            },

            /**
             * Approve a project plan and start execution
             */
            async approveProject(projectId) {
                try {
                    const res = await fetch(`/api/deep-work/projects/${projectId}/approve`, {
                        method: 'POST'
                    });

                    if (res.ok) {
                        const data = await res.json();
                        // Update local project
                        if (this.missionControl.selectedProject?.id === projectId) {
                            this.missionControl.selectedProject = data.project;
                        }
                        const idx = this.missionControl.projects.findIndex(p => p.id === projectId);
                        if (idx >= 0) {
                            this.missionControl.projects[idx] = data.project;
                        }
                        this.showToast('Project approved! Execution started.', 'success');

                        // Brief delay to let background tasks start, then reload
                        // (mc_task_started WebSocket events will also update in real-time)
                        await new Promise(r => setTimeout(r, 500));
                        await this.selectProject(data.project);
                    } else {
                        const err = await res.json();
                        this.showToast(err.detail || 'Failed to approve project', 'error');
                    }
                } catch (e) {
                    console.error('Failed to approve project:', e);
                    this.showToast('Failed to approve project', 'error');
                }
            },

            /**
             * Pause a running project
             */
            async pauseProject(projectId) {
                try {
                    const res = await fetch(`/api/deep-work/projects/${projectId}/pause`, {
                        method: 'POST'
                    });

                    if (res.ok) {
                        const data = await res.json();
                        if (this.missionControl.selectedProject?.id === projectId) {
                            this.missionControl.selectedProject = data.project;
                        }
                        const idx = this.missionControl.projects.findIndex(p => p.id === projectId);
                        if (idx >= 0) {
                            this.missionControl.projects[idx] = data.project;
                        }
                        this.showToast('Project paused', 'info');
                    } else {
                        const err = await res.json();
                        this.showToast(err.detail || 'Failed to pause project', 'error');
                    }
                } catch (e) {
                    console.error('Failed to pause project:', e);
                    this.showToast('Failed to pause project', 'error');
                }
            },

            /**
             * Resume a paused project
             */
            async resumeProject(projectId) {
                try {
                    const res = await fetch(`/api/deep-work/projects/${projectId}/resume`, {
                        method: 'POST'
                    });

                    if (res.ok) {
                        const data = await res.json();
                        if (this.missionControl.selectedProject?.id === projectId) {
                            this.missionControl.selectedProject = data.project;
                        }
                        const idx = this.missionControl.projects.findIndex(p => p.id === projectId);
                        if (idx >= 0) {
                            this.missionControl.projects[idx] = data.project;
                        }
                        this.showToast('Project resumed', 'success');
                        await this.selectProject(data.project);
                    } else {
                        const err = await res.json();
                        this.showToast(err.detail || 'Failed to resume project', 'error');
                    }
                } catch (e) {
                    console.error('Failed to resume project:', e);
                    this.showToast('Failed to resume project', 'error');
                }
            },

            /**
             * Delete a project
             */
            async deleteProject(projectId) {
                if (!confirm('Delete this project and all its tasks?')) return;

                try {
                    const res = await fetch(`/api/mission-control/projects/${projectId}`, {
                        method: 'DELETE'
                    });

                    if (res.ok) {
                        this.missionControl.projects = this.missionControl.projects.filter(p => p.id !== projectId);
                        if (this.missionControl.selectedProject?.id === projectId) {
                            this.missionControl.selectedProject = null;
                        }
                        this.showToast('Project deleted', 'info');
                    }
                } catch (e) {
                    console.error('Failed to delete project:', e);
                    this.showToast('Failed to delete project', 'error');
                }
            },

            // ==================== Output Files ====================

            /**
             * Load output files for the selected project via WebSocket browse command.
             * Uses 'output_' context prefix so file-browser.js routes the response here.
             */
            loadProjectOutputFiles() {
                const project = this.missionControl.selectedProject;
                if (!project || !project.folder_path) return;
                this.missionControl.projectOutputFiles = [];
                this.missionControl.projectOutputLoading = true;
                socket.send('browse', { path: project.folder_path, context: 'output_' + project.id });
            },

            /**
             * Handle browse response routed via EventBus for output_ context.
             */
            handleOutputFiles(data) {
                this.missionControl.projectOutputFiles = data.files || [];
                this.missionControl.projectOutputLoading = false;
                this.$nextTick(() => { if (window.refreshIcons) window.refreshIcons(); });
            },

            /**
             * Open an output file or directory. Directories open in the file browser modal.
             */
            openOutputFile(file) {
                const project = this.missionControl.selectedProject;
                if (!project || !project.folder_path) return;
                const fullPath = project.folder_path + '/' + file.name;
                if (file.isDir) {
                    this.fileLoading = true;
                    this.fileError = null;
                    this.files = [];
                    this.filePath = fullPath;
                    socket.send('browse', { path: fullPath });
                    this.showFileBrowser = true;
                } else {
                    // Open file browser at parent dir so user can see the file in context
                    this.fileLoading = true;
                    this.fileError = null;
                    this.files = [];
                    this.filePath = project.folder_path;
                    socket.send('browse', { path: project.folder_path });
                    this.showFileBrowser = true;
                }
            },

            // ==================== Deep Work Helpers ====================

            /**
             * Get CSS color class for project status
             */
            getProjectStatusColor(status) {
                const colors = {
                    'draft': 'bg-gray-500/20 text-gray-400',
                    'planning': 'bg-blue-500/20 text-blue-400',
                    'awaiting_approval': 'bg-amber-500/20 text-amber-400',
                    'approved': 'bg-cyan-500/20 text-cyan-400',
                    'executing': 'bg-green-500/20 text-green-400',
                    'paused': 'bg-orange-500/20 text-orange-400',
                    'completed': 'bg-emerald-500/20 text-emerald-400',
                    'failed': 'bg-red-500/20 text-red-400'
                };
                return colors[status] || 'bg-white/10 text-white/50';
            },

            /**
             * Get display label for project status
             */
            getProjectStatusLabel(status) {
                const labels = {
                    'draft': 'Draft',
                    'planning': 'Planning...',
                    'awaiting_approval': 'Awaiting Approval',
                    'approved': 'Approved',
                    'executing': 'Executing',
                    'paused': 'Paused',
                    'completed': 'Completed',
                    'failed': 'Failed'
                };
                return labels[status] || status;
            },

            /**
             * Get icon name for project status
             */
            getProjectStatusIcon(status) {
                const icons = {
                    'draft': 'file-edit',
                    'planning': 'brain',
                    'awaiting_approval': 'clock',
                    'approved': 'check-circle',
                    'executing': 'play-circle',
                    'paused': 'pause-circle',
                    'completed': 'check-circle-2',
                    'failed': 'alert-circle'
                };
                return icons[status] || 'circle';
            },

            /**
             * Get planning phase display info
             */
            getPlanningPhaseInfo() {
                const phases = {
                    'starting': { label: 'Initializing', icon: 'loader', step: 0 },
                    'goal_analysis': { label: 'Analyzing Goal', icon: 'target', step: 1 },
                    'research': { label: 'Researching', icon: 'search', step: 2 },
                    'prd': { label: 'Writing PRD', icon: 'file-text', step: 3 },
                    'tasks': { label: 'Breaking Down Tasks', icon: 'list-checks', step: 4 },
                    'team': { label: 'Assembling Team', icon: 'users', step: 5 }
                };
                return phases[this.missionControl.planningPhase] || { label: 'Working', icon: 'loader', step: 0 };
            },

            /**
             * Get domain display info (icon + color)
             */
            getDomainInfo(domain) {
                const domains = {
                    'code': { label: 'Software & Code', icon: 'code-2', color: 'text-blue-400 bg-blue-500/10' },
                    'business': { label: 'Business & Strategy', icon: 'briefcase', color: 'text-amber-400 bg-amber-500/10' },
                    'creative': { label: 'Creative & Content', icon: 'palette', color: 'text-purple-400 bg-purple-500/10' },
                    'education': { label: 'Learning & Education', icon: 'graduation-cap', color: 'text-green-400 bg-green-500/10' },
                    'events': { label: 'Events & Logistics', icon: 'calendar', color: 'text-pink-400 bg-pink-500/10' },
                    'home': { label: 'Home & Physical', icon: 'home', color: 'text-orange-400 bg-orange-500/10' },
                    'hybrid': { label: 'Multi-Domain', icon: 'layers', color: 'text-cyan-400 bg-cyan-500/10' }
                };
                return domains[domain] || { label: domain, icon: 'circle', color: 'text-white/40 bg-white/5' };
            },

            /**
             * Get complexity display info (color + label)
             */
            getComplexityInfo(complexity) {
                const levels = {
                    'S': { label: 'Small', color: 'text-green-400 bg-green-500/10 border-green-500/20' },
                    'M': { label: 'Medium', color: 'text-blue-400 bg-blue-500/10 border-blue-500/20' },
                    'L': { label: 'Large', color: 'text-amber-400 bg-amber-500/10 border-amber-500/20' },
                    'XL': { label: 'Extra Large', color: 'text-red-400 bg-red-500/10 border-red-500/20' }
                };
                return levels[complexity] || { label: complexity, color: 'text-white/40 bg-white/5 border-white/10' };
            },

            /**
             * Get active project count
             */
            getActiveProjectCount() {
                return this.missionControl.projects.filter(p =>
                    ['planning', 'awaiting_approval', 'executing'].includes(p.status)
                ).length;
            },
        };
    }
};

window.PocketPaw.Loader.register('DeepWork', window.PocketPaw.DeepWork);

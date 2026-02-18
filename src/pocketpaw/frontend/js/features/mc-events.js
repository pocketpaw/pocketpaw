/**
 * PocketPaw - Mission Control: Events Module
 *
 * Created: 2026-02-17 — Split from mission-control.js (1,699-line monolith).
 *
 * Contains WebSocket event handling and initial data loading:
 * - loadMCData() — fetches agents, tasks, activity, stats, and projects
 * - handleMCEvent() — handles mc_task_started, mc_task_output,
 *   mc_task_completed, mc_activity_created WebSocket events
 * - handleDWEvent() — handles dw_planning_phase and dw_planning_complete
 *   WebSocket events
 *
 * Also owns the shared "base" state: loading, stats, activities.
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.McEvents = {
    name: 'McEvents',

    getState() {
        return {
            missionControl: {
                loading: false,
                stats: { total_agents: 0, active_tasks: 0, completed_today: 0, total_documents: 0 },
                activities: [],
            }
        };
    },

    getMethods() {
        return {
            // ==================== Mission Control Data Loading ====================

            /**
             * Load Mission Control data from API
             */
            async loadMCData() {
                // Skip if already loaded and not stale
                if (this.missionControl.agents.length > 0 && !this.missionControl.loading) {
                    // Just refresh activity feed
                    try {
                        const activityRes = await fetch('/api/mission-control/activity');
                        if (activityRes.ok) {
                            const data = await activityRes.json();
                            this.missionControl.activities = data.activities || [];
                        }
                    } catch (e) { /* ignore */ }
                    this.$nextTick(() => { if (window.refreshIcons) window.refreshIcons(); });
                    return;
                }

                this.missionControl.loading = true;
                try {
                    const [agentsRes, tasksRes, activityRes, statsRes, projectsRes] = await Promise.all([
                        fetch('/api/mission-control/agents'),
                        fetch('/api/mission-control/tasks'),
                        fetch('/api/mission-control/activity'),
                        fetch('/api/mission-control/stats'),
                        fetch('/api/mission-control/projects')
                    ]);

                    // Unwrap API responses (backend returns {agents: [...], count: N} format)
                    if (agentsRes.ok) {
                        const data = await agentsRes.json();
                        this.missionControl.agents = data.agents || [];
                    }
                    if (tasksRes.ok) {
                        const data = await tasksRes.json();
                        this.missionControl.tasks = data.tasks || [];
                    }
                    if (activityRes.ok) {
                        const data = await activityRes.json();
                        this.missionControl.activities = data.activities || [];
                    }
                    if (statsRes.ok) {
                        const data = await statsRes.json();
                        const raw = data.stats || data;
                        // Map backend stats to frontend format
                        this.missionControl.stats = {
                            total_agents: raw.agents?.total || 0,
                            active_tasks: (raw.tasks?.by_status?.in_progress || 0) + (raw.tasks?.by_status?.assigned || 0),
                            completed_today: raw.tasks?.by_status?.done || 0,
                            total_documents: raw.documents?.total || 0
                        };
                    }
                    if (projectsRes.ok) {
                        const data = await projectsRes.json();
                        this.missionControl.projects = data.projects || [];
                    }
                } catch (e) {
                    console.error('Failed to load Crew data:', e);
                    this.showToast('Failed to load Crew', 'error');
                } finally {
                    this.missionControl.loading = false;
                }
            },

            // ==================== WebSocket Event Handling ====================

            /**
             * Handle Mission Control WebSocket events
             */
            handleMCEvent(data) {
                const eventType = data.event_type;
                const eventData = data.data || {};

                if (eventType === 'mc_task_started') {
                    // Task execution started
                    const taskId = eventData.task_id;
                    const agentId = eventData.agent_id;
                    const agentName = eventData.agent_name;
                    const taskTitle = eventData.task_title;

                    // Track running task
                    this.missionControl.runningTasks[taskId] = {
                        agentId: agentId,
                        agentName: agentName,
                        taskTitle: taskTitle,
                        output: [],
                        startedAt: new Date(),
                        lastAction: 'Starting...'
                    };

                    // Update task status across all lists
                    this._updateTaskInAllLists(taskId, {
                        status: 'in_progress',
                        active_description: `${agentName} is working...`,
                    });

                    // Update agent status
                    const agent = this.missionControl.agents.find(a => a.id === agentId);
                    if (agent) {
                        agent.status = 'active';
                        agent.current_task_id = taskId;
                    }

                    // If this task is selected, clear the live output
                    if (this.missionControl.selectedTask?.id === taskId) {
                        this.missionControl.liveOutput = '';
                    }

                    this.showToast(`${agentName} started: ${taskTitle}`, 'info');
                    this.log(`Task started: ${taskTitle}`, 'info');

                } else if (eventType === 'mc_task_output') {
                    // Agent produced output
                    const taskId = eventData.task_id;
                    const content = eventData.content || '';
                    const outputType = eventData.output_type;

                    // Add to running task output
                    const runningTask = this.missionControl.runningTasks[taskId];
                    if (runningTask) {
                        runningTask.output.push({
                            content,
                            type: outputType,
                            timestamp: new Date()
                        });

                        // Track latest action for inline display
                        if (outputType === 'tool_use') {
                            runningTask.lastAction = content;
                        } else if (outputType === 'message' && content.trim()) {
                            const snippet = content.trim().substring(0, 80);
                            runningTask.lastAction = snippet;
                        }
                    }

                    // Update active_description across all lists for inline visibility
                    if (runningTask) {
                        this._updateTaskInAllLists(taskId, {
                            active_description: runningTask.lastAction || 'Working...',
                        });
                    }

                    // If this task is selected, append to live output
                    if (this.missionControl.selectedTask?.id === taskId) {
                        if (outputType === 'message') {
                            this.missionControl.liveOutput += content;
                        } else if (outputType === 'tool_use') {
                            this.missionControl.liveOutput += `\n\u{1F527} ${content}\n`;
                        } else if (outputType === 'tool_result') {
                            this.missionControl.liveOutput += `\n\u2705 ${content}\n`;
                        }

                        // Scroll live output panel
                        this.$nextTick(() => {
                            const panel = this.$refs.liveOutputPanel;
                            if (panel) panel.scrollTop = panel.scrollHeight;
                        });
                    }

                    // If Agent Activity Sheet is open for this task, scroll it too
                    if (this.missionControl.showAgentActivitySheet &&
                        this.missionControl.activeAgentTask?.taskId === taskId) {
                        this.$nextTick(() => {
                            const panel = this.$refs.agentActivityOutput;
                            if (panel) panel.scrollTop = panel.scrollHeight;
                        });
                    }

                } else if (eventType === 'mc_task_completed') {
                    // Task execution completed
                    const taskId = eventData.task_id;
                    const status = eventData.status;  // 'completed', 'error', 'stopped'
                    const error = eventData.error;

                    // Remove from running tasks
                    delete this.missionControl.runningTasks[taskId];

                    // Update task status across all lists
                    const completedUpdates = {
                        status: status === 'completed' ? 'done' : 'blocked',
                        active_description: null,
                    };
                    if (status === 'completed') {
                        completedUpdates.completed_at = new Date().toISOString();
                    }
                    this._updateTaskInAllLists(taskId, completedUpdates);

                    // For toast message
                    const task = this.missionControl.tasks.find(t => t.id === taskId);

                    // Update agent status
                    const agentId = eventData.agent_id;
                    const agent = this.missionControl.agents.find(a => a.id === agentId);
                    if (agent) {
                        agent.status = 'idle';
                        agent.current_task_id = null;
                    }

                    // Update stats
                    if (status === 'completed') {
                        this.missionControl.stats.completed_today++;
                        this.missionControl.stats.active_tasks = Math.max(0, this.missionControl.stats.active_tasks - 1);
                    }

                    // Refresh project progress if viewing a project
                    if (this.missionControl.selectedProject && status === 'completed') {
                        fetch(`/api/deep-work/projects/${this.missionControl.selectedProject.id}/plan`)
                            .then(r => r.ok ? r.json() : null)
                            .then(progData => {
                                if (progData) {
                                    this.missionControl.projectProgress = progData.progress || null;
                                    this.missionControl.projectTasks = progData.tasks || this.missionControl.projectTasks;
                                    if (this._invalidateTaskCache) this._invalidateTaskCache();
                                }
                            })
                            .catch(() => { /* ignore */ });
                    }

                    // Show notification
                    if (status === 'completed') {
                        this.showToast(`Task completed: ${task?.title || taskId}`, 'success');
                    } else if (status === 'error') {
                        this.showToast(`Task failed: ${error || 'Unknown error'}`, 'error');
                    } else if (status === 'stopped') {
                        this.showToast('Task stopped', 'info');
                    }

                    this.log(`Task ${status}: ${task?.title || taskId}`, status === 'completed' ? 'success' : 'error');

                    // Refresh icons
                    this.$nextTick(() => {
                        if (window.refreshIcons) window.refreshIcons();
                    });

                } else if (eventType === 'mc_activity_created') {
                    // New activity logged
                    const activity = eventData.activity;
                    if (activity) {
                        // Prepend to activities (most recent first)
                        this.missionControl.activities.unshift(activity);
                        // Keep only last 50
                        if (this.missionControl.activities.length > 50) {
                            this.missionControl.activities.pop();
                        }
                    }

                    // Refresh icons for activity feed
                    this.$nextTick(() => {
                        if (window.refreshIcons) window.refreshIcons();
                    });
                }
            },

            // ==================== Deep Work Event Handling ====================

            /**
             * Handle Deep Work WebSocket events
             */
            handleDWEvent(data) {
                const eventType = data.event_type;
                const eventData = data.data || {};

                if (eventType === 'dw_planning_phase') {
                    const projectId = eventData.project_id;
                    const phase = eventData.phase;
                    const message = eventData.message || `Phase: ${phase}`;

                    // Update planning progress
                    if (this.missionControl.planningProjectId === projectId) {
                        this.missionControl.planningPhase = phase;
                        this.missionControl.planningMessage = message;
                    }

                    this.log(`[Deep Work] ${message}`, 'info');

                } else if (eventType === 'dw_planning_complete') {
                    const projectId = eventData.project_id;
                    const status = eventData.status;
                    const title = eventData.title;
                    const error = eventData.error;

                    // Stop planning spinner
                    if (this.missionControl.planningProjectId === projectId) {
                        this.missionControl.projectStarting = false;
                        this.missionControl.planningPhase = '';
                        this.missionControl.planningMessage = '';
                        this.missionControl.planningProjectId = null;
                    }

                    // Update project in list
                    const idx = this.missionControl.projects.findIndex(p => p.id === projectId);
                    if (idx >= 0) {
                        this.missionControl.projects[idx].status = status;
                        if (title) this.missionControl.projects[idx].title = title;
                    }

                    if (status === 'awaiting_approval') {
                        this.showToast('Project plan ready for review!', 'success');

                        // Reload agents list — planning creates new agents
                        fetch('/api/mission-control/agents')
                            .then(r => r.ok ? r.json() : null)
                            .then(agentData => {
                                if (agentData) {
                                    this.missionControl.agents = agentData.agents || [];
                                }
                            })
                            .catch(() => {});

                        // Load the full plan if this project is selected
                        if (this.missionControl.selectedProject?.id === projectId) {
                            this.selectProject({ id: projectId });
                        }
                    } else if (status === 'failed') {
                        this.showToast(`Planning failed: ${error || 'Unknown error'}`, 'error');
                        if (this.missionControl.selectedProject?.id === projectId) {
                            this.missionControl.selectedProject.status = 'failed';
                        }
                    }

                    this.log(`[Deep Work] Planning ${status}: ${title || projectId}`, status === 'failed' ? 'error' : 'success');
                    this.$nextTick(() => { if (window.refreshIcons) window.refreshIcons(); });
                }
            }
        };
    }
};

window.PocketPaw.Loader.register('McEvents', window.PocketPaw.McEvents);

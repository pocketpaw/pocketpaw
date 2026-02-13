/**
 * PocketPaw - Mission Control Feature Module
 *
 * Created: 2026-02-05
 * Updated: 2026-02-12 — Added createProjectTask() for adding tasks to a project.
 *   Enhanced Deep Work task table with:
 *   - Execution levels (phase grouping by dependency order)
 *   - Expandable task rows with inline deliverable preview
 *   - Skip task functionality
 *   - List/timeline view toggle
 *   - Blocker/blocks name resolution helpers
 *   - Task readiness checking for inline run buttons
 *   - handleMCEvent mc_task_completed updates projectTasks
 *
 * Contains all Crew (Mission Control) related state and methods:
 * - Agent CRUD operations
 * - Task CRUD operations
 * - Task execution (run/stop/skip)
 * - WebSocket event handling for real-time updates
 * - Agent Activity Sheet
 * - Deep Work project orchestration
 * - Comments/Thread
 * - Deliverables
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.MissionControl = {
    name: 'MissionControl',
    /**
     * Get initial state for Mission Control
     */
    getState() {
        return {
            missionControl: {
                loading: false,
                taskFilter: 'all',
                agents: [],
                tasks: [],
                activities: [],
                stats: { total_agents: 0, active_tasks: 0, completed_today: 0, total_documents: 0 },
                selectedTask: null,
                showCreateAgent: false,
                showCreateTask: false,
                agentForm: { name: '', role: '', description: '', specialties: '' },
                taskForm: { title: '', description: '', priority: 'medium', assignee: '', tags: '' },
                // Task execution state
                runningTasks: {},  // {task_id: {agentName, agentId, taskTitle, output: [], startedAt}}
                liveOutput: '',    // Current live output for selected task
                // Agent Activity Sheet state
                showAgentActivitySheet: false,
                activeAgentTask: null,  // {taskId, agentId, agentName, taskTitle}
                // Comments/Thread state
                taskMessages: [],
                messageInput: '',
                messagesLoading: false,
                // Deliverables state
                taskDeliverables: [],
                deliverablesLoading: false,

                // Deep Work state
                crewTab: 'tasks',              // 'tasks' | 'projects'
                projects: [],                  // List of projects
                selectedProject: null,         // Currently selected project
                projectTasks: [],              // Tasks for selected project
                projectPrd: null,              // PRD document for selected project
                projectProgress: null,         // {completed, total, percent}
                showStartProject: false,       // Start project modal
                showProjectDetail: false,      // Full project detail sheet
                projectInput: '',              // Natural language project input
                researchDepth: 'standard',     // 'none' | 'quick' | 'standard' | 'deep'
                projectStarting: false,        // Loading state while planner runs
                planningPhase: '',             // Current phase: research, prd, tasks, team
                planningMessage: '',           // Phase progress message
                planningProjectId: null,       // Project being planned

                // Add task to project
                showCreateProjectTask: false,  // Show add-task modal for current project
                projectTaskForm: { title: '', description: '', priority: 'medium', assignee: '', tags: '' },

                // Enhanced task table state
                executionLevels: [],           // list of lists of task IDs from API
                taskLevelMap: {},              // {task_id: level_index}
                expandedTaskId: null,          // which task row is expanded
                taskViewMode: 'list',          // 'list' | 'timeline'
                taskDeliverableCache: {},      // {task_id: [documents...]} for inline preview
            }
        };
    },

    /**
     * Get methods for Mission Control
     * Note: 'this' will be bound to the Alpine component
     */
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

            /**
             * Get filtered tasks based on current filter
             */
            getFilteredMCTasks() {
                const filter = this.missionControl.taskFilter;
                if (filter === 'all') return this.missionControl.tasks;
                return this.missionControl.tasks.filter(t => t.status === filter);
            },

            // ==================== Agent CRUD ====================

            /**
             * Create a new agent
             */
            async createMCAgent() {
                const form = this.missionControl.agentForm;
                if (!form.name || !form.role) return;

                try {
                    const specialties = form.specialties
                        ? form.specialties.split(',').map(s => s.trim()).filter(s => s)
                        : [];

                    const res = await fetch('/api/mission-control/agents', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            name: form.name,
                            role: form.role,
                            description: form.description,
                            specialties: specialties
                        })
                    });

                    if (res.ok) {
                        const data = await res.json();
                        const agent = data.agent || data;  // Unwrap if wrapped
                        this.missionControl.agents.push(agent);
                        this.missionControl.stats.total_agents++;
                        this.missionControl.showCreateAgent = false;
                        this.missionControl.agentForm = { name: '', role: '', description: '', specialties: '' };
                        this.showToast('Agent created!', 'success');
                        this.$nextTick(() => {
                            if (window.refreshIcons) window.refreshIcons();
                        });
                    } else {
                        const err = await res.json();
                        this.showToast(err.detail || 'Failed to create agent', 'error');
                    }
                } catch (e) {
                    console.error('Failed to create agent:', e);
                    this.showToast('Failed to create agent', 'error');
                }
            },

            /**
             * Delete an agent
             */
            async deleteMCAgent(agentId) {
                if (!confirm('Delete this agent?')) return;

                try {
                    const res = await fetch(`/api/mission-control/agents/${agentId}`, {
                        method: 'DELETE'
                    });

                    if (res.ok) {
                        this.missionControl.agents = this.missionControl.agents.filter(a => a.id !== agentId);
                        this.missionControl.stats.total_agents--;
                        this.showToast('Agent deleted', 'info');
                    }
                } catch (e) {
                    console.error('Failed to delete agent:', e);
                    this.showToast('Failed to delete agent', 'error');
                }
            },

            // ==================== Task CRUD ====================

            /**
             * Create a new task
             */
            async createMCTask() {
                const form = this.missionControl.taskForm;
                if (!form.title) return;

                try {
                    const tags = form.tags
                        ? form.tags.split(',').map(s => s.trim()).filter(s => s)
                        : [];

                    const body = {
                        title: form.title,
                        description: form.description,
                        priority: form.priority,
                        tags: tags
                    };

                    if (form.assignee) {
                        body.assignee_ids = [form.assignee];
                    }

                    const res = await fetch('/api/mission-control/tasks', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(body)
                    });

                    if (res.ok) {
                        const data = await res.json();
                        const task = data.task || data;  // Unwrap if wrapped
                        this.missionControl.tasks.unshift(task);
                        this.missionControl.stats.active_tasks++;
                        this.missionControl.showCreateTask = false;
                        this.missionControl.taskForm = { title: '', description: '', priority: 'medium', assignee: '', tags: '' };
                        this.showToast('Task created!', 'success');
                        // Reload activity feed
                        const activityRes = await fetch('/api/mission-control/activity');
                        if (activityRes.ok) {
                            const actData = await activityRes.json();
                            this.missionControl.activities = actData.activities || [];
                        }
                        this.$nextTick(() => {
                            if (window.refreshIcons) window.refreshIcons();
                        });
                    } else {
                        const err = await res.json();
                        this.showToast(err.detail || 'Failed to create task', 'error');
                    }
                } catch (e) {
                    console.error('Failed to create task:', e);
                    this.showToast('Failed to create task', 'error');
                }
            },

            /**
             * Create a task within the currently selected project.
             * Posts to the same /api/mission-control/tasks endpoint with project_id.
             * Refreshes the project plan view after creation.
             */
            async createProjectTask() {
                const form = this.missionControl.projectTaskForm;
                if (!form.title || !this.missionControl.selectedProject) return;

                const projectId = this.missionControl.selectedProject.id;

                try {
                    const tags = form.tags
                        ? form.tags.split(',').map(s => s.trim()).filter(s => s)
                        : [];

                    const body = {
                        title: form.title,
                        description: form.description,
                        priority: form.priority,
                        tags: tags,
                        project_id: projectId
                    };

                    if (form.assignee) {
                        body.assignee_ids = [form.assignee];
                    }

                    const res = await fetch('/api/mission-control/tasks', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(body)
                    });

                    if (res.ok) {
                        const data = await res.json();
                        const task = data.task || data;

                        // Add to project tasks list
                        this.missionControl.projectTasks.push(task);

                        // Reset form and close modal
                        this.missionControl.showCreateProjectTask = false;
                        this.missionControl.projectTaskForm = {
                            title: '', description: '', priority: 'medium',
                            assignee: '', tags: ''
                        };

                        this.showToast('Task added to project!', 'success');

                        // Refresh the full project plan to get updated progress + levels
                        await this.selectProject(this.missionControl.selectedProject);

                        this.$nextTick(() => {
                            if (window.refreshIcons) window.refreshIcons();
                        });
                    } else {
                        const err = await res.json();
                        this.showToast(err.detail || 'Failed to add task', 'error');
                    }
                } catch (e) {
                    console.error('Failed to create project task:', e);
                    this.showToast('Failed to add task', 'error');
                }
            },

            /**
             * Delete a task
             */
            async deleteMCTask(taskId) {
                if (!confirm('Delete this task?')) return;

                try {
                    const res = await fetch(`/api/mission-control/tasks/${taskId}`, {
                        method: 'DELETE'
                    });

                    if (res.ok) {
                        this.missionControl.tasks = this.missionControl.tasks.filter(t => t.id !== taskId);
                        this.missionControl.stats.active_tasks = Math.max(0, this.missionControl.stats.active_tasks - 1);
                        this.showToast('Task deleted', 'info');
                    }
                } catch (e) {
                    console.error('Failed to delete task:', e);
                    this.showToast('Failed to delete task', 'error');
                }
            },

            /**
             * Select a task to show details
             */
            selectMCTask(task) {
                this.missionControl.selectedTask = task;
                this.$nextTick(() => { if (window.refreshIcons) window.refreshIcons(); });
            },

            /**
             * Update task status
             */
            async updateMCTaskStatus(taskId, status) {
                try {
                    const res = await fetch(`/api/mission-control/tasks/${taskId}/status`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ status })
                    });

                    if (res.ok) {
                        const data = await res.json();
                        const serverTask = data.task;

                        this._updateTaskInAllLists(taskId, {
                            status: serverTask.status,
                            completed_at: serverTask.completed_at,
                            updated_at: serverTask.updated_at,
                        });

                        // Refresh project progress if viewing a project
                        if (this.missionControl.selectedProject) {
                            fetch(`/api/deep-work/projects/${this.missionControl.selectedProject.id}/plan`)
                                .then(r => r.ok ? r.json() : null)
                                .then(planData => {
                                    if (planData) {
                                        this.missionControl.projectProgress = planData.progress || null;
                                    }
                                })
                                .catch(() => {});
                        }

                        this.showToast(`Status updated to ${status}`, 'success');

                        // Reload activity
                        const activityRes = await fetch('/api/mission-control/activity');
                        if (activityRes.ok) {
                            const actData = await activityRes.json();
                            this.missionControl.activities = actData.activities || [];
                        }
                    } else {
                        const err = await res.json().catch(() => ({}));
                        this.showToast(err.detail || 'Failed to update status', 'error');
                    }
                } catch (e) {
                    console.error('Failed to update task status:', e);
                    this.showToast('Failed to update status', 'error');
                }
            },

            /**
             * Update task priority
             */
            async updateMCTaskPriority(taskId, priority) {
                try {
                    const res = await fetch(`/api/mission-control/tasks/${taskId}`, {
                        method: 'PATCH',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ priority })
                    });

                    if (res.ok) {
                        this._updateTaskInAllLists(taskId, { priority });
                    }
                } catch (e) {
                    console.error('Failed to update task priority:', e);
                }
            },

            // ==================== Task Update Helper ====================

            /**
             * Atomically update a task across all local lists: tasks, projectTasks,
             * and selectedTask. Prevents sync drift from manual dual-updates.
             *
             * @param {string} taskId - The task ID to update
             * @param {Object} updates - Key/value pairs to set on the task object
             */
            _updateTaskInAllLists(taskId, updates) {
                for (const list of [this.missionControl.tasks, this.missionControl.projectTasks]) {
                    const t = list.find(x => x.id === taskId);
                    if (t) Object.assign(t, updates);
                }
                if (this.missionControl.selectedTask?.id === taskId) {
                    Object.assign(this.missionControl.selectedTask, updates);
                }
            },

            // ==================== Agent Helpers ====================

            /**
             * Get agent initial for avatar
             */
            getAgentInitial(agentId) {
                const agent = this.missionControl.agents.find(a => a.id === agentId);
                return agent ? agent.name.charAt(0).toUpperCase() : '?';
            },

            /**
             * Get agent name by ID
             */
            getAgentName(agentId) {
                const agent = this.missionControl.agents.find(a => a.id === agentId);
                return agent ? agent.name : 'Unknown';
            },

            /**
             * Get full agent object by ID
             */
            getAgentById(agentId) {
                return this.missionControl.agents.find(a => a.id === agentId);
            },

            /**
             * Get agents not already assigned to a task
             */
            getAvailableAgentsForTask(task) {
                if (!task) return this.missionControl.agents;
                const assignedIds = task.assignee_ids || [];
                return this.missionControl.agents.filter(a => !assignedIds.includes(a.id));
            },

            // ==================== Task Assignment ====================

            /**
             * Assign an agent to a task
             */
            async assignAgentToTask(taskId, agentId) {
                if (!taskId || !agentId) return;

                try {
                    const res = await fetch(`/api/mission-control/tasks/${taskId}/assign`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ agent_ids: [agentId] })
                    });

                    if (res.ok) {
                        const data = await res.json();
                        if (data.task) {
                            this._updateTaskInAllLists(taskId, {
                                assignee_ids: data.task.assignee_ids,
                                status: data.task.status,
                            });
                        }
                        this.showToast('Agent assigned', 'success');
                        this.$nextTick(() => { if (window.refreshIcons) window.refreshIcons(); });
                    } else {
                        const err = await res.json();
                        this.showToast(err.detail || 'Failed to assign agent', 'error');
                    }
                } catch (e) {
                    console.error('Failed to assign agent:', e);
                    this.showToast('Failed to assign agent', 'error');
                }
            },

            /**
             * Remove an agent from a task
             */
            async unassignAgentFromTask(taskId, agentId) {
                if (!taskId || !agentId) return;

                try {
                    // Get current assignees and remove this one
                    const task = this.missionControl.tasks.find(t => t.id === taskId);
                    if (!task) return;

                    const newAssignees = (task.assignee_ids || []).filter(id => id !== agentId);

                    const res = await fetch(`/api/mission-control/tasks/${taskId}/assign`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ agent_ids: newAssignees })
                    });

                    if (res.ok) {
                        const data = await res.json();
                        if (data.task) {
                            this._updateTaskInAllLists(taskId, {
                                assignee_ids: data.task.assignee_ids,
                                status: data.task.status,
                            });
                        }
                        this.showToast('Agent removed', 'info');
                        this.$nextTick(() => { if (window.refreshIcons) window.refreshIcons(); });
                    } else {
                        const err = await res.json();
                        this.showToast(err.detail || 'Failed to remove agent', 'error');
                    }
                } catch (e) {
                    console.error('Failed to remove agent:', e);
                    this.showToast('Failed to remove agent', 'error');
                }
            },

            // ==================== Date Formatting ====================

            /**
             * Format date for Mission Control display
             */
            formatMCDate(dateStr) {
                if (!dateStr) return '';
                try {
                    const date = new Date(dateStr);
                    const now = new Date();
                    const diff = now - date;

                    // Less than 1 minute ago
                    if (diff < 60000) return 'Just now';
                    // Less than 1 hour ago
                    if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
                    // Less than 24 hours ago
                    if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;
                    // Otherwise show date
                    return date.toLocaleDateString('en-US', {
                        month: 'short',
                        day: 'numeric'
                    });
                } catch (e) {
                    return dateStr;
                }
            },

            // ==================== Task Execution ====================

            /**
             * Run a task with an assigned agent
             */
            async runMCTask(taskId, agentId) {
                if (!taskId || !agentId) {
                    this.showToast('Task must have an assigned agent', 'error');
                    return;
                }

                // Get task and agent info for immediate UI update
                const task = this.missionControl.tasks.find(t => t.id === taskId);
                const agent = this.missionControl.agents.find(a => a.id === agentId);

                if (!task || !agent) {
                    this.showToast('Task or agent not found', 'error');
                    return;
                }

                try {
                    const res = await fetch(`/api/mission-control/tasks/${taskId}/run`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ agent_id: agentId })
                    });

                    if (res.ok) {
                        const data = await res.json();
                        this.showToast(data.message || 'Task started', 'success');

                        // IMMEDIATELY update local state (don't wait for WebSocket)
                        // Track as running task
                        this.missionControl.runningTasks[taskId] = {
                            agentId: agentId,
                            agentName: agent.name,
                            taskTitle: task.title,
                            output: [],
                            startedAt: new Date(),
                            lastAction: 'Starting...'
                        };

                        // Update task status across all lists
                        this._updateTaskInAllLists(taskId, {
                            status: 'in_progress',
                            started_at: new Date().toISOString(),
                            active_description: `${agent.name} is working...`,
                        });

                        // Update agent status locally
                        agent.status = 'active';
                        agent.current_task_id = taskId;

                        // Update stats
                        this.missionControl.stats.active_tasks++;

                        // Clear and initialize live output
                        this.missionControl.liveOutput = `Starting task with ${agent.name}...\n\n`;

                        // Refresh icons
                        this.$nextTick(() => {
                            if (window.refreshIcons) window.refreshIcons();
                        });
                    } else {
                        const err = await res.json();
                        this.showToast(err.detail || 'Failed to start task', 'error');
                    }
                } catch (e) {
                    console.error('Failed to run task:', e);
                    this.showToast('Failed to start task', 'error');
                }
            },

            /**
             * Stop a running task
             */
            async stopMCTask(taskId) {
                try {
                    const res = await fetch(`/api/mission-control/tasks/${taskId}/stop`, {
                        method: 'POST'
                    });

                    if (res.ok) {
                        this.showToast('Task stopped', 'info');

                        // Immediately update local state
                        const runningData = this.missionControl.runningTasks[taskId];
                        if (runningData) {
                            // Update agent status
                            const agent = this.missionControl.agents.find(a => a.id === runningData.agentId);
                            if (agent) {
                                agent.status = 'idle';
                                agent.current_task_id = null;
                            }
                        }

                        // Remove from running tasks
                        delete this.missionControl.runningTasks[taskId];

                        // Update task status across all lists
                        this._updateTaskInAllLists(taskId, {
                            status: 'blocked',
                            active_description: null,
                        });

                        // Update stats
                        this.missionControl.stats.active_tasks = Math.max(0, this.missionControl.stats.active_tasks - 1);

                        // Close activity sheet if open for this task
                        if (this.missionControl.activeAgentTask?.taskId === taskId) {
                            this.closeAgentActivitySheet();
                        }

                        // Refresh icons
                        this.$nextTick(() => {
                            if (window.refreshIcons) window.refreshIcons();
                        });
                    } else {
                        const err = await res.json();
                        this.showToast(err.detail || 'Failed to stop task', 'error');
                    }
                } catch (e) {
                    console.error('Failed to stop task:', e);
                    this.showToast('Failed to stop task', 'error');
                }
            },

            /**
             * Check if a task is currently running
             */
            isMCTaskRunning(taskId) {
                return taskId in this.missionControl.runningTasks;
            },

            /**
             * Get live output for the selected task
             */
            getMCLiveOutput() {
                return this.missionControl.liveOutput;
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
                            this.missionControl.liveOutput += `\n🔧 ${content}\n`;
                        } else if (outputType === 'tool_result') {
                            this.missionControl.liveOutput += `\n✅ ${content}\n`;
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

            // ==================== Agent Activity Sheet ====================

            /**
             * Get the first running task (for the banner display)
             */
            getFirstRunningTask() {
                const runningTaskIds = Object.keys(this.missionControl.runningTasks);
                if (runningTaskIds.length === 0) return null;

                const taskId = runningTaskIds[0];
                const runningData = this.missionControl.runningTasks[taskId];
                const task = this.missionControl.tasks.find(t => t.id === taskId);

                return {
                    taskId: taskId,
                    agentName: runningData?.agentName || 'Agent',
                    agentId: runningData?.agentId,
                    taskTitle: task?.title || runningData?.taskTitle || 'Task',
                    startedAt: runningData?.startedAt,
                    outputCount: runningData?.output?.length || 0
                };
            },

            /**
             * Get count of running tasks
             */
            getRunningTaskCount() {
                return Object.keys(this.missionControl.runningTasks).length;
            },

            /**
             * Open the Agent Activity Sheet for a specific task
             */
            openAgentActivitySheet(taskId) {
                const runningData = this.missionControl.runningTasks[taskId];
                const task = this.missionControl.tasks.find(t => t.id === taskId);

                if (!runningData && !task) return;

                this.missionControl.activeAgentTask = {
                    taskId: taskId,
                    agentId: runningData?.agentId,
                    agentName: runningData?.agentName || 'Agent',
                    taskTitle: task?.title || 'Task',
                    startedAt: runningData?.startedAt
                };
                this.missionControl.showAgentActivitySheet = true;

                // Auto-scroll output on open
                this.$nextTick(() => {
                    const panel = this.$refs.agentActivityOutput;
                    if (panel) panel.scrollTop = panel.scrollHeight;
                    if (window.refreshIcons) window.refreshIcons();
                });
            },

            /**
             * Close the Agent Activity Sheet
             */
            closeAgentActivitySheet() {
                this.missionControl.showAgentActivitySheet = false;
                this.missionControl.activeAgentTask = null;
            },

            /**
             * Get full output for the Agent Activity Sheet
             */
            getAgentActivityOutput(taskId) {
                const runningData = this.missionControl.runningTasks[taskId];
                if (!runningData || !runningData.output) return 'Waiting for output...';

                return runningData.output.map(chunk => {
                    if (chunk.type === 'tool_use') {
                        return `🔧 ${chunk.content}`;
                    } else if (chunk.type === 'tool_result') {
                        return `✅ ${chunk.content}`;
                    }
                    return chunk.content;
                }).join('');
            },

            /**
             * Format elapsed time since task started
             */
            formatElapsedTime(startedAt) {
                if (!startedAt) return '';
                const start = new Date(startedAt);
                const now = new Date();
                const diff = Math.floor((now - start) / 1000);

                if (diff < 60) return `${diff}s`;
                if (diff < 3600) return `${Math.floor(diff / 60)}m ${diff % 60}s`;
                return `${Math.floor(diff / 3600)}h ${Math.floor((diff % 3600) / 60)}m`;
            },

            // ==================== Comments/Thread ====================

            /**
             * Load messages for a task
             */
            async loadTaskMessages(taskId) {
                if (!taskId) return;

                this.missionControl.messagesLoading = true;
                this.missionControl.taskMessages = [];

                try {
                    const res = await fetch(`/api/mission-control/tasks/${taskId}/messages`);
                    if (res.ok) {
                        const data = await res.json();
                        this.missionControl.taskMessages = data.messages || [];
                    }
                } catch (e) {
                    console.error('Failed to load messages:', e);
                } finally {
                    this.missionControl.messagesLoading = false;
                    this.$nextTick(() => {
                        const panel = this.$refs.taskMessagesPanel;
                        if (panel) panel.scrollTop = panel.scrollHeight;
                        if (window.refreshIcons) window.refreshIcons();
                    });
                }
            },

            /**
             * Post a message to a task thread
             */
            async postTaskMessage(taskId) {
                const content = this.missionControl.messageInput.trim();
                if (!content || !taskId) return;

                try {
                    // Use 'human' as a special agent ID for human messages
                    const res = await fetch(`/api/mission-control/tasks/${taskId}/messages`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            from_agent_id: 'human',
                            content: content,
                            attachment_ids: []
                        })
                    });

                    if (res.ok) {
                        const data = await res.json();
                        this.missionControl.taskMessages.push(data.message);
                        this.missionControl.messageInput = '';

                        // Scroll to bottom
                        this.$nextTick(() => {
                            const panel = this.$refs.taskMessagesPanel;
                            if (panel) panel.scrollTop = panel.scrollHeight;
                        });
                    } else {
                        const err = await res.json();
                        this.showToast(err.detail || 'Failed to post message', 'error');
                    }
                } catch (e) {
                    console.error('Failed to post message:', e);
                    this.showToast('Failed to post message', 'error');
                }
            },

            // ==================== Deliverables ====================

            /**
             * Load deliverables (documents) for a task
             */
            async loadTaskDeliverables(taskId) {
                if (!taskId) return;

                this.missionControl.deliverablesLoading = true;
                this.missionControl.taskDeliverables = [];

                try {
                    const res = await fetch(`/api/mission-control/tasks/${taskId}/documents`);
                    if (res.ok) {
                        const data = await res.json();
                        this.missionControl.taskDeliverables = data.documents || [];
                    }
                } catch (e) {
                    console.error('Failed to load deliverables:', e);
                } finally {
                    this.missionControl.deliverablesLoading = false;
                    this.$nextTick(() => {
                        if (window.refreshIcons) window.refreshIcons();
                    });
                }
            },

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
             * Start a new Deep Work project from natural language input
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
                    const res = await fetch('/api/deep-work/start', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            description: input,
                            research_depth: this.missionControl.researchDepth
                        })
                    });

                    if (res.ok) {
                        const data = await res.json();
                        const project = data.project;
                        this.missionControl.projects.unshift(project);
                        this.missionControl.projectInput = '';
                        this.missionControl.showStartProject = false;

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
                this.missionControl.projectPrd = null;
                this.missionControl.projectProgress = null;
                this.missionControl.executionLevels = [];
                this.missionControl.taskLevelMap = {};
                this.missionControl.expandedTaskId = null;
                this.missionControl.taskDeliverableCache = {};

                try {
                    const res = await fetch(`/api/deep-work/projects/${project.id}/plan`);
                    if (res.ok) {
                        const data = await res.json();
                        // Update project from server (may be newer)
                        this.missionControl.selectedProject = data.project;
                        this.missionControl.projectTasks = data.tasks || [];
                        this.missionControl.projectProgress = data.progress || null;
                        this.missionControl.projectPrd = data.prd || null;
                        this.missionControl.executionLevels = data.execution_levels || [];
                        this.missionControl.taskLevelMap = data.task_level_map || {};

                        // Also update in projects list
                        const idx = this.missionControl.projects.findIndex(p => p.id === project.id);
                        if (idx >= 0) {
                            this.missionControl.projects[idx] = data.project;
                        }
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
                    'research': { label: 'Researching', icon: 'search', step: 1 },
                    'prd': { label: 'Writing PRD', icon: 'file-text', step: 2 },
                    'tasks': { label: 'Breaking Down Tasks', icon: 'list-checks', step: 3 },
                    'team': { label: 'Assembling Team', icon: 'users', step: 4 }
                };
                return phases[this.missionControl.planningPhase] || { label: 'Working', icon: 'loader', step: 0 };
            },

            /**
             * Get active project count
             */
            getActiveProjectCount() {
                return this.missionControl.projects.filter(p =>
                    ['planning', 'awaiting_approval', 'executing'].includes(p.status)
                ).length;
            },

            // ==================== Enhanced Task Table Helpers ====================

            /**
             * Get tasks grouped by execution level for phase-based rendering.
             * Returns array of {level, tasks} objects.
             */
            getTasksByLevel() {
                const levels = this.missionControl.executionLevels;
                const allTasks = this.missionControl.projectTasks;
                if (!levels || levels.length === 0) {
                    // Fallback: single group with all tasks
                    return [{ level: 0, tasks: allTasks }];
                }

                const taskMap = {};
                for (const t of allTasks) {
                    taskMap[t.id] = t;
                }

                return levels.map((taskIds, idx) => ({
                    level: idx,
                    tasks: taskIds.map(id => taskMap[id]).filter(Boolean)
                }));
            },

            /**
             * Resolve blocked_by IDs to task titles for display.
             */
            getBlockerNames(task) {
                if (!task.blocked_by || task.blocked_by.length === 0) return [];
                return task.blocked_by.map(id => {
                    const t = this.missionControl.projectTasks.find(pt => pt.id === id);
                    return t ? t.title : id.substring(0, 8);
                });
            },

            /**
             * Resolve blocks IDs to task titles for display.
             */
            getBlocksNames(task) {
                if (!task.blocks || task.blocks.length === 0) return [];
                return task.blocks.map(id => {
                    const t = this.missionControl.projectTasks.find(pt => pt.id === id);
                    return t ? t.title : id.substring(0, 8);
                });
            },

            /**
             * Check if a task is ready to run (all blockers done or skipped).
             */
            isTaskReady(task) {
                if (!task.blocked_by || task.blocked_by.length === 0) return true;
                return task.blocked_by.every(id => {
                    const dep = this.missionControl.projectTasks.find(t => t.id === id);
                    return dep && (dep.status === 'done' || dep.status === 'skipped');
                });
            },

            /**
             * Toggle expand/collapse for a task row. Lazy-loads deliverables.
             */
            toggleTaskExpand(taskId) {
                if (this.missionControl.expandedTaskId === taskId) {
                    this.missionControl.expandedTaskId = null;
                    return;
                }
                this.missionControl.expandedTaskId = taskId;

                // Lazy load deliverables for completed tasks
                const task = this.missionControl.projectTasks.find(t => t.id === taskId);
                if (task && (task.status === 'done' || task.status === 'skipped') && !this.missionControl.taskDeliverableCache[taskId]) {
                    this.loadTaskDeliverablesInline(taskId);
                }

                this.$nextTick(() => { if (window.refreshIcons) window.refreshIcons(); });
            },

            /**
             * Fetch deliverables for inline preview (first 500 chars).
             */
            async loadTaskDeliverablesInline(taskId) {
                try {
                    const res = await fetch(`/api/mission-control/tasks/${taskId}/documents`);
                    if (res.ok) {
                        const data = await res.json();
                        this.missionControl.taskDeliverableCache[taskId] = data.documents || [];
                    }
                } catch (e) {
                    console.error('Failed to load inline deliverables:', e);
                    this.missionControl.taskDeliverableCache[taskId] = [];
                }
            },

            /**
             * Skip a project task — mark as skipped, unblock dependents.
             */
            async skipProjectTask(taskId) {
                const project = this.missionControl.selectedProject;
                if (!project) return;

                try {
                    const res = await fetch(`/api/deep-work/projects/${project.id}/tasks/${taskId}/skip`, {
                        method: 'POST'
                    });

                    if (res.ok) {
                        const data = await res.json();

                        // Update local task state
                        const task = this.missionControl.projectTasks.find(t => t.id === taskId);
                        if (task) {
                            task.status = 'skipped';
                            task.completed_at = data.task.completed_at;
                        }

                        // Update progress
                        if (data.progress) {
                            this.missionControl.projectProgress = data.progress;
                        }

                        // Refresh the full project to see unblocked tasks
                        await this.selectProject(project);

                        this.showToast('Task skipped', 'info');
                    } else {
                        const err = await res.json();
                        this.showToast(err.detail || 'Failed to skip task', 'error');
                    }
                } catch (e) {
                    console.error('Failed to skip task:', e);
                    this.showToast('Failed to skip task', 'error');
                }
            },

            /**
             * Get maximum estimated_minutes across all project tasks (for timeline bar scaling).
             */
            getMaxEstimatedMinutes() {
                const tasks = this.missionControl.projectTasks;
                if (!tasks || tasks.length === 0) return 30;
                const max = Math.max(...tasks.map(t => t.estimated_minutes || 0));
                return max || 30;
            },

            /**
             * Get timeline bar color based on task status.
             */
            getTimelineBarColor(status) {
                const colors = {
                    'inbox': 'bg-blue-500/50',
                    'assigned': 'bg-cyan-500/50',
                    'in_progress': 'bg-amber-500/70',
                    'review': 'bg-purple-500/50',
                    'done': 'bg-green-500/60',
                    'blocked': 'bg-red-500/40',
                    'skipped': 'bg-gray-500/40'
                };
                return colors[status] || 'bg-white/15';
            },

            /**
             * Get timeline status icon name.
             */
            getTimelineStatusIcon(status) {
                const icons = {
                    'inbox': 'circle',
                    'assigned': 'user-check',
                    'in_progress': 'loader',
                    'review': 'eye',
                    'done': 'check',
                    'blocked': 'lock',
                    'skipped': 'skip-forward'
                };
                return icons[status] || 'circle';
            },

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

window.PocketPaw.Loader.register('MissionControl', window.PocketPaw.MissionControl);

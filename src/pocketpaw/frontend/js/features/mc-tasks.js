/**
 * PocketPaw - Mission Control: Tasks Module
 *
 * Created: 2026-02-17 — Split from mission-control.js (1,699-line monolith).
 * Updated: 2026-02-17 — Added cached task lookup map and dirty-flag caching
 *   for getTasksByLevel to avoid redundant computation on every render cycle.
 *
 * Contains task-related state and methods:
 * - Task CRUD (create, delete, update status/priority)
 * - Task filtering and selection
 * - Task execution (run/stop/skip)
 * - Task update helper (_updateTaskInAllLists)
 * - Comments/Thread (load, post messages)
 * - Deliverables (load documents)
 * - Enhanced task table helpers (execution levels, blocker names, timeline)
 * - Project task creation (createProjectTask)
 * - Date formatting
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.McTasks = {
    name: 'McTasks',

    getState() {
        return {
            missionControl: {
                taskFilter: 'all',
                tasks: [],
                selectedTask: null,
                showCreateTask: false,
                taskForm: { title: '', description: '', priority: 'medium', assignee: '', tags: '' },
                // Task execution state
                runningTasks: {},  // {task_id: {agentName, agentId, taskTitle, output: [], startedAt}}
                liveOutput: '',    // Current live output for selected task
                // Comments/Thread state
                taskMessages: [],
                messageInput: '',
                messagesLoading: false,
                // Deliverables state
                taskDeliverables: [],
                deliverablesLoading: false,
                // Add task to project
                showCreateProjectTask: false,
                projectTaskForm: { title: '', description: '', priority: 'medium', assignee: '', tags: '' },
                // Enhanced task table state
                executionLevels: [],           // list of lists of task IDs from API
                taskLevelMap: {},              // {task_id: level_index}
                expandedTaskId: null,          // which task row is expanded
                taskViewMode: 'list',          // 'list' | 'timeline'
                taskDeliverableCache: {},      // {task_id: [documents...]} for inline preview
            },
            // Computed cache (top-level — methods access these as this._taskMapCache etc.)
            _taskMapCache: null,           // {task_id: task} lookup, invalidated on task changes
            _taskMapVersion: 0,            // incremented when tasks change
            _levelsCacheVersion: -1,       // version at which levels cache was built
            _levelsCache: null,            // cached getTasksByLevel result
        };
    },

    getMethods() {
        return {
            // ==================== Cache helpers ====================

            /**
             * Invalidate task caches. Call this after any mutation to tasks or projectTasks.
             */
            _invalidateTaskCache() {
                this._taskMapCache = null;
                this._taskMapVersion++;
            },

            /**
             * Get a {task_id: task} lookup map for projectTasks. Cached until invalidated.
             */
            _getProjectTaskMap() {
                if (!this._taskMapCache) {
                    this._taskMapCache = {};
                    for (const t of (this.missionControl.projectTasks || [])) {
                        this._taskMapCache[t.id] = t;
                    }
                }
                return this._taskMapCache;
            },

            // ==================== Task CRUD ====================

            /**
             * Get filtered tasks based on current filter
             */
            getFilteredMCTasks() {
                const filter = this.missionControl.taskFilter;
                if (filter === 'all') return this.missionControl.tasks;
                return this.missionControl.tasks.filter(t => t.status === filter);
            },

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
                        this._invalidateTaskCache();

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
                this._invalidateTaskCache();
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

            // ==================== Enhanced Task Table Helpers ====================

            /**
             * Get tasks grouped by execution level for phase-based rendering.
             * Returns array of {level, tasks} objects.
             */
            getTasksByLevel() {
                // Return cached result if tasks haven't changed
                if (this._levelsCache && this._levelsCacheVersion === this._taskMapVersion) {
                    return this._levelsCache;
                }

                const levels = this.missionControl.executionLevels;
                const allTasks = this.missionControl.projectTasks;
                if (!levels || levels.length === 0) {
                    this._levelsCache = [{ level: 0, tasks: allTasks }];
                } else {
                    const taskMap = this._getProjectTaskMap();
                    this._levelsCache = levels.map((taskIds, idx) => ({
                        level: idx,
                        tasks: taskIds.map(id => taskMap[id]).filter(Boolean)
                    }));
                }

                this._levelsCacheVersion = this._taskMapVersion;
                return this._levelsCache;
            },

            /**
             * Resolve blocked_by IDs to task titles for display.
             */
            getBlockerNames(task) {
                if (!task.blocked_by || task.blocked_by.length === 0) return [];
                const map = this._getProjectTaskMap();
                return task.blocked_by.map(id => {
                    const t = map[id];
                    return t ? t.title : id.substring(0, 8);
                });
            },

            /**
             * Resolve blocks IDs to task titles for display.
             */
            getBlocksNames(task) {
                if (!task.blocks || task.blocks.length === 0) return [];
                const map = this._getProjectTaskMap();
                return task.blocks.map(id => {
                    const t = map[id];
                    return t ? t.title : id.substring(0, 8);
                });
            },

            /**
             * Check if a task is ready to run (all blockers done or skipped).
             */
            isTaskReady(task) {
                if (!task.blocked_by || task.blocked_by.length === 0) return true;
                const map = this._getProjectTaskMap();
                return task.blocked_by.every(id => {
                    const dep = map[id];
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
        };
    }
};

window.PocketPaw.Loader.register('McTasks', window.PocketPaw.McTasks);

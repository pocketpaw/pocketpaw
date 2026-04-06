/**
 * PocketPaw - Mission Control: Agents Module
 *
 * Created: 2026-02-17 — Split from mission-control.js (1,699-line monolith).
 *
 * Contains agent-related state and methods:
 * - Agent CRUD (create, delete)
 * - Agent lookup helpers (getAgentInitial, getAgentName, getAgentById)
 * - Agent assignment to tasks
 * - Agent Activity Sheet (live view of running agent output)
 * - Running task banner helpers (getFirstRunningTask, getRunningTaskCount)
 * - Time formatting helpers
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.McAgents = {
    name: 'McAgents',

    getState() {
        return {
            missionControl: {
                agents: [],
                agentForm: { name: '', role: '', description: '', specialties: '' },
                showCreateAgent: false,
                showAgentActivitySheet: false,
                activeAgentTask: null,  // {taskId, agentId, agentName, taskTitle}
            }
        };
    },

    getMethods() {
        return {
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
                        return `\u{1F527} ${chunk.content}`;
                    } else if (chunk.type === 'tool_result') {
                        return `\u2705 ${chunk.content}`;
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
        };
    }
};

window.PocketPaw.Loader.register('McAgents', window.PocketPaw.McAgents);

/**
 * PocketPaw - Skills Feature Module
 *
 * Created: 2026-02-05
 * Updated: 2026-02-12 — Two-view modal (My Skills + Library), REST-based, skills.sh integration.
 *
 * Contains skill-related state and methods:
 * - Skill listing, filtering, and management
 * - Skills.sh library search and install
 * - Skill execution and command parsing
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.Skills = {
    name: 'Skills',
    /**
     * Get initial state for Skills
     */
    getState() {
        return {
            showSkills: false,
            skills: [],
            skillsLoading: false,
            // Two-view state
            skillsView: 'installed',        // 'installed' | 'library'
            skillSearchQuery: '',            // filter installed skills
            // Library state
            libraryQuery: '',                // search skills.sh
            libraryResults: [],              // skills.sh search results
            libraryLoading: false,
            skillInstalling: null,           // source string currently being installed
            libraryFeatured: []              // initial popular skills
        };
    },

    /**
     * Get methods for Skills
     */
    getMethods() {
        return {
            /**
             * Handle skills list (legacy WS handler, kept for run_skill responses)
             */
            handleSkills(data) {
                this.skills = data.skills || [];
                this.skillsLoading = false;
                this.$nextTick(() => {
                    if (window.refreshIcons) window.refreshIcons();
                });
            },

            /**
             * Handle skill started
             */
            handleSkillStarted(data) {
                this.showToast(`Running: ${data.skill_name}`, 'info');
                this.log(`Skill started: ${data.skill_name}`, 'info');
            },

            /**
             * Handle skill completed
             */
            handleSkillCompleted(data) {
                this.log(`Skill completed: ${data.skill_name}`, 'success');
            },

            /**
             * Handle skill error
             */
            handleSkillError(data) {
                this.showToast(`Skill error: ${data.error}`, 'error');
                this.log(`Skill error: ${data.error}`, 'error');
            },

            /**
             * Open skills panel — fetch installed via REST
             */
            async openSkills() {
                this.showSkills = true;
                this.skillsLoading = true;

                try {
                    const res = await fetch('/api/skills');
                    if (res.ok) {
                        this.skills = await res.json();
                    }
                } catch (e) {
                    console.error('Failed to load skills', e);
                } finally {
                    this.skillsLoading = false;
                    this.$nextTick(() => {
                        if (window.refreshIcons) window.refreshIcons();
                    });
                }
            },

            /**
             * Filter installed skills by search query (client-side)
             */
            filteredSkills() {
                if (!this.skillSearchQuery) return this.skills;
                const q = this.skillSearchQuery.toLowerCase();
                return this.skills.filter(s =>
                    s.name.toLowerCase().includes(q) ||
                    (s.description || '').toLowerCase().includes(q)
                );
            },

            /**
             * Search skills.sh library (debounced via Alpine @input.debounce)
             */
            async searchLibrary() {
                const q = this.libraryQuery.trim();
                if (!q) {
                    this.libraryResults = [];
                    return;
                }

                this.libraryLoading = true;
                try {
                    const res = await fetch(`/api/skills/search?q=${encodeURIComponent(q)}&limit=30`);
                    if (res.ok) {
                        const data = await res.json();
                        this.libraryResults = data.skills || [];
                    }
                } catch (e) {
                    console.error('Library search failed', e);
                } finally {
                    this.libraryLoading = false;
                    this.$nextTick(() => {
                        if (window.refreshIcons) window.refreshIcons();
                    });
                }
            },

            /**
             * Load featured/popular skills for initial library view
             */
            async loadFeatured() {
                this.libraryLoading = true;
                try {
                    const res = await fetch('/api/skills/search?q=sk&limit=20');
                    if (res.ok) {
                        const data = await res.json();
                        this.libraryFeatured = data.skills || [];
                    }
                } catch (e) {
                    console.error('Failed to load featured skills', e);
                } finally {
                    this.libraryLoading = false;
                    this.$nextTick(() => {
                        if (window.refreshIcons) window.refreshIcons();
                    });
                }
            },

            /**
             * Get the list to display in library view (search results or featured)
             */
            libraryDisplayResults() {
                return this.libraryQuery.trim()
                    ? this.libraryResults
                    : this.libraryFeatured;
            },

            /**
             * Check if a library skill is already installed locally
             */
            isSkillInstalled(librarySkill) {
                const name = (librarySkill.name || librarySkill.skillId || '').toLowerCase();
                return this.skills.some(s => s.name.toLowerCase() === name);
            },

            /**
             * Install a skill from the library
             */
            async installSkill(source) {
                if (!source) return;
                this.skillInstalling = source;

                try {
                    const res = await fetch('/api/skills/install', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ source })
                    });
                    const data = await res.json();
                    if (res.ok && data.status === 'ok') {
                        const names = (data.installed || []).join(', ');
                        this.showToast(names ? `Installed: ${names}` : 'Skill installed!', 'success');
                        // Reload installed skills
                        const reload = await fetch('/api/skills');
                        if (reload.ok) {
                            this.skills = await reload.json();
                        }
                    } else {
                        this.showToast(data.error || 'Install failed', 'error');
                    }
                } catch (e) {
                    this.showToast('Install failed: ' + e.message, 'error');
                } finally {
                    this.skillInstalling = null;
                    this.$nextTick(() => {
                        if (window.refreshIcons) window.refreshIcons();
                    });
                }
            },

            /**
             * Remove an installed skill
             */
            async removeSkill(name) {
                try {
                    const res = await fetch('/api/skills/remove', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ name })
                    });
                    const data = await res.json();
                    if (res.ok && data.status === 'ok') {
                        this.showToast(`Skill "${name}" removed`, 'info');
                        const reload = await fetch('/api/skills');
                        if (reload.ok) {
                            this.skills = await reload.json();
                        }
                    } else {
                        this.showToast(data.error || 'Remove failed', 'error');
                    }
                } catch (e) {
                    this.showToast('Remove failed: ' + e.message, 'error');
                }
            },

            /**
             * Format install count for display (e.g. 1234 -> "1.2K")
             */
            formatInstalls(n) {
                if (!n) return '';
                if (n >= 1000000) return (n / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
                if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'K';
                return String(n);
            },

            /**
             * Run a skill
             */
            runSkill(name, args = '') {
                this.showSkills = false;
                socket.send('run_skill', { name, args });
                this.log(`Running skill: ${name} ${args}`, 'info');
            },

            /**
             * Check if input is a skill command and run it
             */
            checkSkillCommand(text) {
                if (text.startsWith('/')) {
                    const parts = text.slice(1).split(' ');
                    const skillName = parts[0];
                    const args = parts.slice(1).join(' ');

                    // Check if skill exists
                    const skill = this.skills.find(s => s.name === skillName);
                    if (skill) {
                        this.runSkill(skillName, args);
                        return true;
                    }
                }
                return false;
            }
        };
    }
};

window.PocketPaw.Loader.register('Skills', window.PocketPaw.Skills);

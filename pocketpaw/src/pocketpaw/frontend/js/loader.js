/**
 * PocketPaw - Feature Module Loader
 *
 * Created: 2026-02-11
 * Updated: 2026-02-17 — assemble() now deep-merges one level of nesting so
 *   multiple modules can contribute to the same top-level state key
 *   (e.g. missionControl).
 *
 * Auto-discovers and assembles feature modules into the Alpine.js app.
 * Feature modules self-register via PocketPaw.Loader.register(name, module).
 *
 * Each module must expose:
 *   - getState()   -> object of reactive Alpine data
 *   - getMethods() -> object of methods mixed into the app
 *
 * Usage in a feature module:
 *   window.PocketPaw.Loader.register('MyFeature', {
 *       getState()   { return { ... }; },
 *       getMethods() { return { ... }; }
 *   });
 *
 * Usage in app.js:
 *   const { state, methods } = window.PocketPaw.Loader.assemble();
 */

window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.Loader = (() => {
    /** @type {Map<string, {getState: Function, getMethods: Function}>} */
    const _modules = new Map();

    return {
        /**
         * Register a feature module.
         *
         * @param {string} name   - Unique module name (e.g. 'Chat', 'Sessions')
         * @param {object} module - Object with getState() and getMethods()
         */
        register(name, module) {
            if (_modules.has(name)) {
                console.warn(`[Loader] Module "${name}" already registered — overwriting`);
            }
            _modules.set(name, module);
        },

        /**
         * Assemble all registered modules into merged state and methods.
         *
         * @returns {{ state: object, methods: object }}
         */
        assemble() {
            const state = {};
            const methods = {};

            for (const [name, mod] of _modules) {
                if (typeof mod.getState === 'function') {
                    const modState = mod.getState();
                    for (const key of Object.keys(modState)) {
                        if (
                            state[key] &&
                            typeof state[key] === 'object' &&
                            !Array.isArray(state[key]) &&
                            typeof modState[key] === 'object' &&
                            !Array.isArray(modState[key])
                        ) {
                            // Deep-merge one level: merge into existing object
                            Object.assign(state[key], modState[key]);
                        } else {
                            state[key] = modState[key];
                        }
                    }
                }
                if (typeof mod.getMethods === 'function') {
                    Object.assign(methods, mod.getMethods());
                }
            }

            return { state, methods };
        },

        /**
         * Check if a module is registered.
         *
         * @param {string} name
         * @returns {boolean}
         */
        has(name) {
            return _modules.has(name);
        },

        /**
         * Get list of registered module names (useful for debugging).
         *
         * @returns {string[]}
         */
        list() {
            return [..._modules.keys()];
        }
    };
})();

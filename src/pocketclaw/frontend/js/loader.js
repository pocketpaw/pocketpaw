/**
 * PocketPaw - Feature Module Loader
 *
 * Created: 2026-02-11
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
                    Object.assign(state, mod.getState());
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

/**
 * PocketPaw - State Manager
 *
 * Created: 2026-02-10
 *
 * Manages localStorage persistence and in-memory session cache.
 * - save/load/remove: localStorage with "pw_" prefix
 * - cacheSession/getCachedSession/invalidateSession: in-memory LRU (max 20)
 */

const StateManager = (() => {
    const PREFIX = 'pw_';
    const MAX_CACHE = 20;
    const _cache = new Map();

    return {
        save(key, value) {
            try {
                localStorage.setItem(PREFIX + key, JSON.stringify(value));
            } catch (e) {
                console.warn('[State] save failed:', e);
            }
        },

        load(key, fallback = null) {
            try {
                const raw = localStorage.getItem(PREFIX + key);
                return raw !== null ? JSON.parse(raw) : fallback;
            } catch (e) {
                return fallback;
            }
        },

        remove(key) {
            localStorage.removeItem(PREFIX + key);
        },

        cacheSession(id, messages) {
            // Evict oldest if at capacity
            if (_cache.size >= MAX_CACHE && !_cache.has(id)) {
                const oldest = _cache.keys().next().value;
                _cache.delete(oldest);
            }
            // Move to end (most recent)
            _cache.delete(id);
            _cache.set(id, [...messages]);
        },

        getCachedSession(id) {
            const data = _cache.get(id);
            return data ? [...data] : null;
        },

        invalidateSession(id) {
            _cache.delete(id);
        }
    };
})();

window.StateManager = StateManager;

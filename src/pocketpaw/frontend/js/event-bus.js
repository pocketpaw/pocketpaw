/**
 * PocketPaw Event Bus - Lightweight pub/sub for decoupled module communication
 *
 * Created: 2026-02-17
 *
 * Usage:
 *   PocketPaw.EventBus.on('output:files_ready', ({ projectId, files }) => { ... });
 *   PocketPaw.EventBus.emit('output:files_ready', { projectId: 'abc', files: [] });
 *   PocketPaw.EventBus.on('*', (event, data) => console.log(event, data)); // debug
 *   PocketPaw.EventBus.off('output:files_ready', handler);
 */
window.PocketPaw = window.PocketPaw || {};

window.PocketPaw.EventBus = (() => {
  const listeners = {};

  function on(event, handler) {
    (listeners[event] = listeners[event] || []).push(handler);
  }

  function off(event, handler) {
    const list = listeners[event];
    if (!list) return;
    listeners[event] = list.filter((h) => h !== handler);
  }

  function emit(event, data) {
    (listeners[event] || []).forEach((h) => h(data));
    if (event !== '*') {
      (listeners['*'] || []).forEach((h) => h(event, data));
    }
  }

  return { on, off, emit };
})();

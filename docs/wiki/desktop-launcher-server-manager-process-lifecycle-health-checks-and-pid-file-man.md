---
{
  "title": "Desktop Launcher Server Manager: Process Lifecycle, Health Checks, and PID File Management",
  "summary": "The `ServerManager` class controls the lifecycle of the PocketPaw FastAPI server as a subprocess launched from the desktop launcher. It handles start, stop, restart, health polling, stale PID file cleanup, and dynamic port allocation — all designed to survive launcher restarts and OS-level process deaths.",
  "concepts": [
    "ServerManager",
    "subprocess",
    "PID file",
    "health check",
    "port allocation",
    "SIGTERM",
    "SIGKILL",
    "graceful shutdown",
    "threading lock",
    "CREATE_NO_WINDOW",
    "process lifecycle",
    "stale PID cleanup"
  ],
  "categories": [
    "installer",
    "launcher",
    "process-management",
    "server"
  ],
  "source_docs": [
    "49bea613e71f63e6"
  ],
  "backlinks": null,
  "word_count": 542,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`installer/launcher/server.py` wraps the PocketPaw server subprocess in a managed object that the tray icon and other launcher components can call. It is the boundary between the GUI world (tray, splash) and the running server process. The design assumes the server may die unexpectedly, the launcher may be restarted while the server is running, and the user may change port settings between launches.

## Process Start and Lock

`start()` delegates to `_start_locked()`, which acquires a `threading.Lock` before spawning the process. The lock prevents concurrent start calls — for example, if the user clicks the tray menu quickly, two `start()` calls could race and launch two server processes. Only one subprocess is ever tracked in `self._process`.

`_creation_flags()` returns `CREATE_NO_WINDOW` on Windows (same motivation as in bootstrap: suppresses console flicker in a GUI context).

## PID File Management

A PID file at `POCKETPAW_HOME / "launcher.pid"` is written on start and read on stop/status checks. This allows the launcher to find and signal a server process that outlived a previous launcher session. The `is_running()` method:

1. Checks `self._process` (in-memory handle, only valid in current session)
2. Falls back to `_stop_via_pid()` which reads the PID file
3. Calls `_pid_alive()` to verify the PID is still a live process
4. **Cleans up stale PID files** if the process is no longer running

Stale PID cleanup was added (noted in the module header: `is_running() now cleans up stale PID files`) because early versions left dead PID files that caused the launcher to report the server as running when it was not — leading to confusing tray states.

## Health Checking

`is_healthy()` sends an HTTP GET to the `/health` endpoint on the configured port. This is more reliable than checking whether the process is alive: the process could be running but stuck in startup, deadlocked, or listening on the wrong port. The `_wait_for_healthy(timeout)` method polls at 500ms intervals until the health check passes or the timeout expires — this is called after `start()` so the tray icon only shows "running" once the server is actually ready to serve requests.

## Port Management

`self.port` is resolved from a config file first, then falls back to `DEFAULT_PORT = 8888`. `_find_free_port()` binds to port 0 (OS-assigned) and reads back the assigned port — a standard technique for finding an available port without races. `_is_port_free()` does a quick socket bind test used before attempting to start the server on a specific port.

## Graceful Shutdown

`_graceful_shutdown()` first sends `SIGTERM` and waits up to `timeout` seconds for the process to exit cleanly. If the process does not exit, it sends `SIGKILL`. This two-phase approach allows the server's shutdown handler to close database connections and flush buffers before being force-killed. On Windows, `SIGTERM` is not supported the same way; the method uses `proc.terminate()` which maps to `TerminateProcess` on that platform.

## Known Gaps

- Log file rotation is not implemented; `self._log_fh` captures server stdout/stderr to a single file that grows unbounded.
- `_stop_via_pid()` does not verify that the process identified by the PID is actually a PocketPaw process — a recycled PID could cause the launcher to signal an unrelated process.
- The port configuration read from the config file is not validated; a non-integer value would raise at runtime rather than at startup.
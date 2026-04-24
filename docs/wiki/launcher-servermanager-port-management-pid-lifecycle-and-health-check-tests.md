---
{
  "title": "Launcher ServerManager: Port Management, PID Lifecycle, and Health Check Tests",
  "summary": "This test module covers `ServerManager`, the launcher component responsible for managing the PocketPaw FastAPI server process: detecting free ports, reading port configuration, managing PID files, performing health checks, and cleanly starting and stopping the server process.",
  "concepts": [
    "ServerManager",
    "port management",
    "PID file",
    "stale PID",
    "health check",
    "find_free_port",
    "is_running",
    "config reading",
    "start stop",
    "dashboard URL",
    "process lifecycle",
    "launcher server"
  ],
  "categories": [
    "testing",
    "launcher",
    "process management",
    "server lifecycle",
    "port management",
    "test"
  ],
  "source_docs": [
    "a890c336c0a4cb96"
  ],
  "backlinks": null,
  "word_count": 518,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ServerManager` is the bridge between the GUI launcher and the PocketPaw server process. It handles all the operational concerns that arise when managing a background process: port conflicts, stale PID files, health polling, and graceful shutdown. The tests mock `socket`, `subprocess`, and filesystem operations to verify these behaviors without a real server.

## `TestPortManagement`

Port management prevents startup failures when the default port is already in use:

- **`test_is_port_free_available`** — creating a socket connection to the port fails with `ConnectionRefusedError` → port is free.
- **`test_is_port_free_taken`** — connection succeeds → port is occupied.
- **`test_find_free_port_default_available`** — if the default port (8080 or configured) is free, `find_free_port()` returns it.
- **`test_find_free_port_default_taken`** — if the default is taken, the function scans upward (8081, 8082, ...) until it finds a free port. `mock_free(port)` controls which ports the mock reports as free.

Without this logic, if another service occupies the default port, the PocketPaw server would fail with a cryptic `OSError: [Errno 98] Address already in use`.

## `TestConfigReading`

The server port is persisted in the config file so the GUI and tray icon can locate the running server:

- **`test_read_port_from_config`** — reads a valid `{"port": 8080}` from a JSON config file.
- **`test_read_port_no_config`** — config file does not exist → returns `None` (not an error).
- **`test_read_port_invalid_json`** — corrupt config → returns `None` gracefully. A corrupt config must not crash the launcher at startup.
- **`test_read_port_no_port_key`** — config exists but lacks a `port` key → returns `None`. Prevents `KeyError` crashes.

## `TestPidManagement`

PID files track the running server process. Stale PID files (left over after a crash) must be detected and ignored:

- **`test_is_running_no_process_no_pid`** — no PID file and no running process → `is_running()` returns `False`.
- **`test_is_running_with_active_process`** — mocks `psutil.Process` (or equivalent) to report the PID as alive → returns `True`.
- **`test_is_running_dead_process`** — the PID in the file belongs to a dead process (raises `ProcessLookupError` or equivalent) → returns `False`.
- **`test_is_running_stale_pid_file`** — the PID file exists but the process is gone → returns `False`. Without stale PID detection, the launcher would refuse to start a new server because it thinks one is already running.

## `TestHealthCheck`

- **`test_healthy_server`** — `GET /health` returns 200 → server is healthy.
- **`test_unhealthy_server`** — connection refused or non-200 response → server is not healthy.
- **`test_dashboard_url`** — verifies the dashboard URL format (`http://localhost:{port}/`) returned by `ServerManager` for the tray icon to open.

## `TestStartStop`

- **`test_start_no_python`** — when no Python executable is found in the venv, `start()` must fail with an informative error rather than raising an unhandled exception.
- **`test_start_already_running`** — if `is_running()` returns `True`, `start()` must be a no-op. Starting a second server process would cause a port conflict.
- **`test_stop_cleans_pid`** — after `stop()`, the PID file must be deleted. A surviving PID file would make `is_running()` return stale results.

## Known Gaps

No tests cover the case where the server process starts but never becomes healthy (health check keeps timing out). No tests verify that `start()` correctly passes environment variables (API keys, config path) to the server subprocess. No tests cover SIGTERM vs. SIGKILL escalation during `stop()`.
---
{
  "title": "Desktop Launcher Splash Window: First-Run Progress UI with Threaded Installation",
  "summary": "The `SplashWindow` class provides a lightweight tkinter progress window shown during first-run installation. It runs the bootstrap install function in a background thread so the UI remains responsive, and uses `after()` polling to safely update the progress bar from the main thread.",
  "concepts": [
    "SplashWindow",
    "tkinter",
    "progress bar",
    "threading",
    "background thread",
    "after()",
    "cross-thread update",
    "first-run experience",
    "install callback",
    "done polling",
    "WM_DELETE_WINDOW"
  ],
  "categories": [
    "installer",
    "launcher",
    "ui",
    "desktop"
  ],
  "source_docs": [
    "e50108e0ef40b95c"
  ],
  "backlinks": null,
  "word_count": 490,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`installer/launcher/splash.py` exists because the bootstrap process (Python detection, uv download, package install) can take 30–60 seconds on first run. Without a progress window, the app would appear to hang. The implementation uses only `tkinter`, which ships with the Python standard library — this is a deliberate constraint that keeps the launcher's dependency footprint at zero before the venv is set up.

## Threading Model

The core challenge is that tkinter is not thread-safe: only the main thread may update widgets. The installation, however, must run in a background thread so it does not block the event loop. `SplashWindow` solves this with a two-part design:

1. `_run_install()` runs in a `threading.Thread`, calls the injected `install_fn`, and sets `self._done = True` (or stores an error in `self._error`) when finished.
2. `update_progress()` is the callback handed to `install_fn`. It calls `self._root.after(0, self._do_update, message, percent)`, which schedules `_do_update` to run on the main thread at the next event loop iteration.

This pattern (`after(0, fn)`) is the standard tkinter technique for cross-thread widget updates. Using it prevents race conditions and segfaults that occur when background threads touch tkinter widgets directly.

## Install Callback Interface

```python
def run(self, install_fn: Callable[[Callable[[str, int], None]], None]) -> bool:
```

The `install_fn` receives a single argument: the progress callback `(message: str, percent: int) -> None`. This interface matches the `progress` parameter that `Bootstrap.run()` already accepts, so wiring them together requires no adaptation layer.

## Done Polling

`_check_done()` is scheduled with `after(500, _check_done)` — it re-schedules itself every 500ms until `self._done` is True. When done, it calls `_show_error()` (if an error occurred) or `_close()` (on success). This polling approach avoids thread joins that would block the event loop.

## Error Display

`_show_error()` replaces the progress bar with an error message label and a "Close" button. The error text comes from `self._error`, which the background thread sets before marking `_done = True`. The window stays open on error so the user can read the message — it does not auto-close.

## Window Centering

The window is centered on screen by reading `winfo_screenwidth()` and `winfo_screenheight()` before the event loop starts. `winfo_` methods require the window to exist but not necessarily to be visible, so this must happen after `tk.Tk()` is created but before `mainloop()` is called.

## Close Guard

`_on_close()` (the `WM_DELETE_WINDOW` protocol handler) is a no-op during installation — the window cannot be closed until the install thread completes. This prevents the user from dismissing the splash mid-install, which would leave the venv in a partial state.

## Known Gaps

- The window has no cancel button; there is no mechanism to abort a running install short of killing the process.
- The progress bar uses percentage integers; if `install_fn` never calls the callback with `percent=100`, the bar may stop at a value below 100 even on success.
- Error messages are displayed as raw exception strings, which may include file paths or stack frames not suitable for end users.
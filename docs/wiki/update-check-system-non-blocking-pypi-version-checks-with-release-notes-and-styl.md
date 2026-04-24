---
{
  "title": "Update Check System: Non-Blocking PyPI Version Checks with Release Notes and Styled CLI Notices",
  "summary": "The update check module queries PyPI once per 24 hours to determine whether a newer version of PocketPaw is available, caches the result to a local file, and prints a styled terminal box on TTY sessions when an update is found. It additionally fetches per-version release notes from GitHub with a separate 1-hour cache and tracks which version the user has last acknowledged.",
  "concepts": [
    "update check",
    "PyPI",
    "version comparison",
    "cache",
    "ThreadPoolExecutor",
    "styled CLI",
    "release notes",
    "GitHub API",
    "mark_version_seen",
    "ANSI colors",
    "suppression conditions",
    "atexit"
  ],
  "categories": [
    "developer experience",
    "CLI",
    "release management"
  ],
  "source_docs": [
    "872158981a045b3c"
  ],
  "backlinks": null,
  "word_count": 450,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw is distributed via PyPI, and users may run older versions indefinitely without knowing updates are available. The `update_check.py` module solves this visibility problem with a minimal-impact notification system: it checks PyPI daily using a cache to avoid per-launch network overhead, and surfaces updates through a styled terminal notice that does not pollute piped output or CI environments.

## Cache-First Architecture

The core `check_for_updates()` function reads a `.update_check` JSON file from the PocketPaw config directory before making any network request. The cache includes a timestamp (`ts`) and the latest version string. If the cache is fresh (under 24 hours old), the function returns immediately from the cache -- no network call, no latency.

On a cache miss or corruption, the function fetches `https://pypi.org/pypi/pocketpaw/json`, parses the `info.version` field, and writes the result back to the cache. The entire function is wrapped in a broad `except Exception` with a debug-level log -- update check failures are silently absorbed so they never interrupt the user's workflow.

## Async Wrapper

```python
async def check_for_updates_async(current_version, config_dir):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_HTTP_EXECUTOR, check_for_updates, ...)
```

The synchronous `check_for_updates` runs in a dedicated `ThreadPoolExecutor` to avoid blocking the asyncio event loop during the network call. The executor uses `atexit` to shut down cleanly on process exit without waiting for in-flight requests.

## Version Comparison

Version strings are parsed into tuples of integers via `_parse_version()`, which handles pre-release suffixes (e.g., `0.4.1rc1`) by stripping non-numeric characters. Tuple comparison (`(0, 4, 2) > (0, 4, 1)`) is then used for version ordering -- a simple approach that avoids importing `packaging.version` and its transitive dependencies.

## Styled Terminal Notice

`print_styled_update_notice()` uses box-drawing Unicode characters and ANSI color codes to render a visually prominent update box on stderr. Three suppression conditions are checked first:

- `POCKETPAW_NO_UPDATE_CHECK` environment variable is set
- `CI` environment variable is set (suppresses in GitHub Actions, CircleCI, etc.)
- `sys.stderr.isatty()` returns False (suppresses in non-interactive contexts)

## Release Notes Fetching

`fetch_release_notes()` calls the GitHub Releases API to retrieve the changelog body for a specific version. Results are cached per-version with a 1-hour TTL in a separate `.release_notes_cache/` directory. This cache is separate from the update check cache because release notes are immutable once published.

## Version Seen Tracking

`mark_version_seen()` and `get_last_seen_version()` allow the dashboard or CLI to track which version the user has already acknowledged, preventing the same update notice from appearing on every launch after the user has already seen it.

## Known Gaps

The `print_styled_update_notice` function hardcodes column widths using magic numbers that assume specific content lengths. If the changelog URL or upgrade command string changes length, the box borders will misalign. `print_update_notice` is marked deprecated but still delegates to the styled version.
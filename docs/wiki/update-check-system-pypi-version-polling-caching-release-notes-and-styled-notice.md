---
{
  "title": "Update Check System: PyPI Version Polling, Caching, Release Notes, and Styled Notices",
  "summary": "The update check module polls PyPI for new PocketPaw versions, caches results to avoid repeated network calls, fetches release notes from GitHub, tracks which versions the user has already seen, and displays styled terminal notices. Tests cover version parsing, cache freshness/staleness, network error handling, corrupted cache recovery, async wrapping, TTY detection for notice suppression, and the version-seen tracking mechanism.",
  "concepts": [
    "update_check",
    "PyPI",
    "version_parsing",
    "cache_TTL",
    "async_wrapper",
    "release_notes",
    "version_seen",
    "styled_notice",
    "TTY_detection",
    "CACHE_FILENAME"
  ],
  "categories": [
    "update-system",
    "testing",
    "developer-experience",
    "test"
  ],
  "source_docs": [
    "1e106f81f6960eb9"
  ],
  "backlinks": null,
  "word_count": 455,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw checks PyPI for available updates in the background and notifies users via a styled terminal banner. The system is designed to be non-blocking (async wrapper), resilient to network failures, and respectful of CI environments where terminal output is inappropriate.

## Version Parsing

`_parse_version` converts a semantic version string to a comparable tuple: `"0.4.1"` → `(0, 4, 1)`. This simple tuple comparison is used instead of a packaging library to avoid adding a dependency. Tests cover single-digit, multi-digit, and two-digit minor versions.

## Update Detection with Caching

`check_for_updates(current_version, cache_dir)` polls `https://pypi.org/pypi/pocketpaw/json` and returns a result dict with `current`, `latest`, and `update_available` keys. The function writes a cache file (`CACHE_FILENAME`) after each successful network call.

Cache TTL (`CACHE_TTL`) controls freshness: a fresh cache is used directly without a network call, while a stale cache (older than TTL) triggers a new poll. This prevents hammering PyPI on every PocketPaw startup.

```python
def test_uses_fresh_cache(tmp_path):
    # Pre-write a cache file with mtime=now → should not call urlopen

def test_ignores_stale_cache(tmp_path):
    # Pre-write cache with old mtime → should call urlopen
```

## Resilience to Network Failures and Corrupted Caches

- **Network error**: Returns `None` rather than raising, allowing callers to treat a failed check as "no update info available".
- **Corrupted cache**: If the cache JSON is malformed, it is ignored and a fresh network call is attempted. This handles the case where a power interruption truncated the cache file.

## Async Wrapper

`check_for_updates_async` wraps the synchronous PyPI poll in a thread pool executor so it does not block the async event loop. The test verifies it returns the same shape as the sync version and correctly reports `update_available=True` when the latest version is newer.

## Styled Update Notice

`print_styled_update_notice` renders a box-drawn terminal banner using Unicode box-drawing characters. It is suppressed in several contexts:
- **Non-TTY environments** (`sys.stderr.isatty()` returns False)
- **CI environments** (`CI` env var is set)
- **Explicit opt-out** via a PocketPaw-specific env var

This prevents update notices from polluting CI log output or piped scripts.

## Release Notes Fetching

`fetch_release_notes` retrieves the changelog from GitHub and caches it in `RELEASE_NOTES_CACHE_DIR`. The cache prevents repeated GitHub calls for the same version. Network failures return `None` gracefully.

## Version Seen Tracking

`mark_version_seen(version)` records that the user has acknowledged a version, and `get_last_seen_version()` retrieves it. This prevents showing the same update notice on every startup once the user has seen it. Tests verify:
- Initial state returns `None` (no prior version seen)
- `mark_version_seen` persists the version
- Existing cache entries for other data are preserved when marking a version seen
- Subsequent marks update the stored version correctly

## Known Gaps

No TODOs. The `test_async_wrapper` test is not decorated with `@pytest.mark.asyncio`, relying on pytest-asyncio's auto-detection mode.
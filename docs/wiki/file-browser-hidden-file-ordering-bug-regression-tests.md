---
{
  "title": "File Browser Hidden File Ordering Bug Regression Tests",
  "summary": "This test file reproduces and documents a bug in `handle_file_browse` where applying the `items[:50]` slice before the hidden-file filter caused all visible files to disappear from results when a directory contained 50 or more dot-prefixed items. The tests were created on 2026-02-12 to lock in the correct behavior — filter first, then slice.",
  "concepts": [
    "handle_file_browse",
    "hidden file filter",
    "items[:50] slice",
    "file browser bug",
    "dot-prefixed files",
    "WebSocket",
    "dashboard",
    "file jail",
    "regression test",
    "directory listing"
  ],
  "categories": [
    "testing",
    "dashboard",
    "bug regression",
    "test"
  ],
  "source_docs": [
    "3ac6b5ccb610a307"
  ],
  "backlinks": null,
  "word_count": 487,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## The Bug

PocketPaw's dashboard file browser uses `handle_file_browse` to list directory contents via WebSocket. The original implementation read all `os.scandir` entries, applied `items[:50]` to cap the result, and then filtered out hidden files (names starting with `.`). In a directory with 55 hidden subdirectories and 5 visible ones, the 50-item cap consumed all hidden entries first (they sort alphabetically before uppercase visible names), leaving zero items for the visible-file filter to return. From the user's perspective, a non-empty directory appeared completely empty.

The fix is to filter hidden files first, then apply the 50-item cap.

## Test Infrastructure

Two fixtures are provided:

- **`mock_websocket`** — an `AsyncMock` with a `send_json` method that appends each payload to `ws.sent_messages`, allowing assertions on exactly what was sent.
- **`mock_settings`** — a `MagicMock` with `file_jail_path` set to `tmp_path`, so all paths remain within the temporary directory.

## Test Cases

### test_empty_result_with_many_hidden_dirs (Bug Reproducer)

This is the primary regression test. It creates 55 hidden directories (`.hidden_dir_00` through `.hidden_dir_54`) and 5 visible directories (`Applications`, `Desktop`, `Documents`, `Downloads`, `Music`), then calls `handle_file_browse`.

```python
assert len(files) > 0, (
    "Expected visible directories but got empty list. "
    "This is the bug: items[:50] applied before hidden file filtering."
)
for name in visible_dirs:
    assert name in file_names
```

The error message embedded in the assertion is intentionally descriptive so that if this test fails in the future, the developer immediately understands what went wrong.

### test_visible_files_returned_when_few_hidden (Baseline)

With only 5 hidden items and 5 visible directories plus 2 visible files, the old code worked correctly. This test confirms the fix did not break the common case and that exactly 5 visible items are returned (verifying no accidental over-filtering).

### test_hidden_files_never_included

Verifies that hidden items (`.gitconfig`, `.ssh`) are always excluded regardless of directory size. This guards against a regression where the filter might be removed or inverted.

### test_limit_applies_to_visible_items

The most precise behavioral test: with 30 hidden and 60 visible directories, exactly 50 visible items should be returned. Before the fix, only 20 visible items would appear (50 - 30 hidden that consumed the cap). The assertion message `"The limit should apply after filtering hidden files"` makes the intent clear.

```python
assert len(files) == 50, (
    f"Expected 50 visible items but got {len(files)}. "
    f"The limit should apply after filtering hidden files."
)
```

## Why Ordering Matters

Filesystem entries on macOS and Linux sort with dotfiles first when using alphabetical ordering, but the behavior depends on the OS locale and filesystem. The bug was latent — it only manifested when the number of hidden items reached or exceeded the cap. The tests use deterministic construction (predictable names, controlled counts) to make the failure reproducible.

## Known Gaps

Tests do not cover symlinks that point to hidden directories, or directories where the 50-item cap itself should be configurable. There is also no test for the WebSocket error path when `handle_file_browse` receives a path outside the jail.
---
{
  "title": "Fetch Tool Path Security and Jail Boundary Tests",
  "summary": "This test module validates the security model of PocketPaw's file fetch tool, specifically the `is_safe_path` predicate and `handle_path`/`list_directory` handlers that enforce a configurable jail directory. The tests were created as regression guards for issue #619, where an empty path string could bypass the jail check by resolving to the current working directory.",
  "concepts": [
    "path jail",
    "is_safe_path",
    "handle_path",
    "list_directory",
    "FetchRequest",
    "path traversal",
    "empty string bypass",
    "issue #619",
    "security regression",
    "file access control",
    "Path.resolve"
  ],
  "categories": [
    "testing",
    "security",
    "file tools",
    "test"
  ],
  "source_docs": [
    "e65e188f66ece49e"
  ],
  "backlinks": null,
  "word_count": 503,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why These Tests Exist

PocketPaw's fetch tool allows agents to read files from the user's filesystem. Without a boundary, an agent could read `/etc/passwd`, private SSH keys, or any other sensitive file. The jail mechanism constrains all access to a designated directory tree. Issue #619 revealed that an empty string `""` passed as a path would silently resolve to the process's current working directory rather than being rejected — a bypass that would let an attacker escape the jail with a trivially crafted request.

This test file exists to document the fix and prevent regressions on both the path-level predicate (`is_safe_path`) and the higher-level request handlers (`handle_path`, `list_directory`).

## TestIsSafePath

Four unit tests cover the core predicate:

- **Within jail** — a subdirectory of the jail root is safe.
- **Outside jail** — a sibling directory at the same level as the jail root is not safe.
- **At jail root** — the root itself is safe (equality is included).
- **Sibling directory** — a sibling created alongside `jail_dir/` is unsafe, preventing traversal to `../sibling`.

The predicate uses `Path.is_relative_to()` (or equivalent) to check ancestry after resolving symlinks. Checking resolved paths matters because `../` sequences and symlinks can otherwise escape the jail.

## Empty-String Rejection (Issue #619 Regression)

```python
async def test_handle_path_empty_string_rejected() -> None:
    result = await handle_path("", Path.home())
    assert result["type"] == "error"
    assert "Validation Error" in result["message"]
```

Before the fix, `Path("").resolve()` would return `Path.cwd()`. If `cwd` happened to be inside the jail, the request would silently succeed and expose whatever the process's working directory contained. If `cwd` was outside the jail, the error message would leak the resolved path, aiding reconnaissance.

The fix validates that the path string is not empty or whitespace *before* any `Path` resolution occurs. `FetchRequest` raises `ValueError: Path string cannot be empty or whitespace` at construction time, so neither the jail check nor the filesystem ever sees the string.

Three handlers are tested for this guard:
- `handle_path("", ...)` — returns `{"type": "error", "message": "Validation Error ..."}`
- `handle_path("   ", ...)` — whitespace-only is equally rejected
- `list_directory("", ...)` — returns a string containing `"Validation Error"`

## Outside-Jail Rejection

Beyond the empty-string case, tests confirm that an absolute path pointing outside the jail returns `"Access denied"` from both `handle_path` and `list_directory`. This is the main line of defense for path traversal attacks using `../../` sequences.

## TestSecurityRegressions Class

Two tests are explicitly labeled as regression tests for issue #619:

- `test_empty_path_cannot_bypass_jail` — end-to-end: an empty path through `handle_path` returns an error.
- `test_path_resolve_with_empty_string_not_called` — unit test for `FetchRequest` directly, asserting that constructing it with `path_str=""` raises `ValueError` before any `Path` object is ever created.

This two-level approach (validator + handler) ensures the fix is robust even if someone bypasses the `FetchRequest` layer.

## Known Gaps

No `TODO` or `FIXME` markers are present. Tests cover ASCII path manipulation but do not test Unicode null bytes (`%00`) or percent-encoded traversal sequences, which are relevant if the path comes from an HTTP query string rather than directly from agent code.
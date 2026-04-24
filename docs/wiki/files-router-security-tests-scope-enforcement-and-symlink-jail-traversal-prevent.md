---
{
  "title": "Files Router Security Tests: Scope Enforcement and Symlink Jail Traversal Prevention",
  "summary": "Security regression tests for issues #884 and #886 in PocketPaw's files router: #884 proved any API key (even one with no scopes) could read arbitrary files; #886 proved `/files/download-zip` followed symlinks outside the configured file jail. These tests lock in the fixes and prevent both vulnerabilities from regressing.",
  "concepts": [
    "scope enforcement",
    "file jail",
    "symlink traversal",
    "path traversal",
    "download-zip",
    "API key scopes",
    "security regression",
    "issue #884",
    "issue #886",
    "file browsing security",
    "access control"
  ],
  "categories": [
    "testing",
    "security",
    "file system",
    "access control",
    "test"
  ],
  "source_docs": [
    "01e8387456e866be"
  ],
  "backlinks": null,
  "word_count": 423,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_api_v1_files_security.py` is a security-focused regression test file that targets two specific vulnerabilities in PocketPaw's file browsing API. The tests were added on 2026-04-16 and exist specifically to prevent regressions on two confirmed security issues.

## Issue #884: Scope Enforcement

`TestScopeEnforcement` proves that the files router correctly enforces API key scope requirements. Before the fix, any API key — even one with an empty `scopes` list — could call file endpoints and read arbitrary files from the host filesystem.

### Fixture Design

`app_with_scopeless_apikey` builds a FastAPI app with custom HTTP middleware that injects a `_Key` object with `self.scopes = []` (intentionally empty) into `request.state.api_key`. This simulates a valid but unprivileged API key — one that has passed authentication but has no authorized scopes.

`jailed_settings` patches `settings.file_jail_path` to point at a temp directory, ensuring no tests can accidentally browse real filesystem paths.

### Endpoint Coverage

Six endpoints are tested:
- `GET /files/browse`
- `GET /files/content`
- `GET /files/download`
- `POST /files/download-zip`
- `GET /files/recent`
- `POST /files/open`

Each test asserts the response is `403 Forbidden`. Before the fix, these returned `200 OK` with file data.

**Failure it prevents:** A scopeless API key obtained by an attacker (e.g., through a leaked token or a compromised integration) could exfiltrate arbitrary files from the PocketPaw host machine without any scope grant.

## Issue #886: Symlink Path Traversal

`TestSymlinkFilter` proves that `/files/download-zip` does not follow symlinks that point outside the file jail.

### Test Setup

`test_zip_skips_symlink_pointing_outside_jail` creates a directory structure where:
- `jailed_dir/` is the configured jail root.
- `jailed_dir/safe.txt` is a legitimate file inside the jail.
- `jailed_dir/escape.txt` is a symlink pointing to `/etc/passwd` (outside the jail).

The test requests a zip of both files and verifies the resulting zip contains `safe.txt` but does NOT contain `escape.txt`.

### Why Symlink Filtering Matters

Without symlink filtering, an attacker who can create a symlink inside the jail (e.g., through a tool call or agent action) can exfiltrate any file on the host by including it in a download-zip request. The fix filters out any zip entry whose resolved path falls outside the jail root.

The test uses a custom `_inject` middleware to set `request.state.api_key` and `request.state.oauth_token` with appropriate scopes so the endpoint passes auth checks and the test isolates symlink behavior specifically.

## Known Gaps

No TODO or FIXME markers. The tests cover the specific reported vulnerabilities but do not cover:
- Hardlink traversal (hardlinks bypass symlink checks).
- Directory junction traversal on Windows.
- Zip slip attacks (path traversal via `../` entries inside a zip being extracted).
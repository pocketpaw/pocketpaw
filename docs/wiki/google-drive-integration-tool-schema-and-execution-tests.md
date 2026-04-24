---
{
  "title": "Google Drive Integration Tool Schema and Execution Tests",
  "summary": "This test module validates the four Google Drive tools (`DriveListTool`, `DriveDownloadTool`, `DriveUploadTool`, `DriveShareTool`), their parameter schemas, and their behavior when authentication is absent or inputs are invalid. It also verifies `DriveListTool`'s happy path with a mocked HTTP response and confirms that `DriveShareTool` rejects invalid sharing roles at the tool layer.",
  "concepts": [
    "DriveListTool",
    "DriveDownloadTool",
    "DriveUploadTool",
    "DriveShareTool",
    "Google Drive",
    "DriveClient",
    "OAuth",
    "role validation",
    "trust_level",
    "httpx",
    "file upload",
    "file sharing"
  ],
  "categories": [
    "testing",
    "Google integrations",
    "tools",
    "test"
  ],
  "source_docs": [
    "f499b09c71b3aef3"
  ],
  "backlinks": null,
  "word_count": 483,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's Google Drive integration (Sprint 25) exposes four agent tools that let the AI assistant read, write, and share files on the user's Drive:

- **`DriveListTool`** — lists files in Drive, optionally filtered by a query string.
- **`DriveDownloadTool`** — downloads a file by its ID.
- **`DriveUploadTool`** — uploads a local file to Drive.
- **`DriveShareTool`** — shares a file with an email address at a specified role.

## Tool Schema Tests (TestDriveToolSchemas)

Four tests validate names, trust levels, and required parameters:

- `DriveListTool` — name `"drive_list"`, trust `"high"` (reading all files is privileged).
- `DriveDownloadTool` — requires `file_id` in both `properties` and `required`.
- `DriveUploadTool` — requires `file_path`.
- `DriveShareTool` — accepts `file_id`, `email`, and `role` parameters.

## Auth Error Tests

Four tools are tested with `DriveClient._get_token` mocked to raise `RuntimeError("Not authenticated")`:

- `test_drive_list_no_auth` — result starts with `"Error:"` and contains `"authenticated"`.
- `test_drive_download_no_auth` — result starts with `"Error:"`.
- `test_drive_upload_no_auth` — result starts with `"Error:"` (auth check runs before file-not-found check).
- `test_drive_share_no_auth` — result starts with `"Error:"`.

Auth checks must run before any I/O to avoid leaking filesystem information (file existence) to an unauthenticated caller.

## Input Validation Tests

**`test_drive_upload_file_not_found`** — with a valid mock token, passing a nonexistent file path returns `"Error: ... not found"`. This test verifies that the tool handles the case gracefully rather than propagating a Python `FileNotFoundError`.

**`test_drive_share_invalid_role`** — `DriveShareTool` validates the `role` parameter at the tool layer before making any API call:

```python
async def test_drive_share_invalid_role():
    tool = DriveShareTool()
    result = await tool.execute(file_id="abc", email="x@y.com", role="admin")
    assert result.startswith("Error:")
    assert "Invalid role" in result
```

Google Drive's sharing API accepts only `"reader"`, `"commenter"`, and `"writer"`. Passing `"admin"` would produce a cryptic API error. The validation at the tool layer produces an agent-readable message that the user can act on.

## Happy Path Test (test_drive_list_success)

This test mocks both `_get_token` and `httpx.AsyncClient` to simulate a successful file listing:

```python
mock_resp.json.return_value = {"files": [{"id": "abc123", "name": "report.pdf", ...}]}
```

The result string is expected to contain `"report.pdf"` and `"abc123"`, verifying that the tool formats the response for agent consumption.

**`test_drive_list_empty`** — when the API returns `{"files": []}`, the result contains `"No files found"`, giving the agent a clear signal to relay to the user rather than returning an empty string.

## Why Role Validation Belongs at the Tool Layer

API errors from Google contain OAuth-style error codes that are not user-friendly. By validating the role at the tool layer (before any network call), PocketPaw ensures the agent receives a clear, actionable error message. The list of valid roles should be kept in sync with the Google Drive API documentation; there is no automated enforcement of this today.

## Known Gaps

No tests cover `DriveDownloadTool` in the success path, binary file downloads, or files in shared drives. The `role` allowlist is hardcoded in the tool and not tested for completeness against the Google Drive API specification.
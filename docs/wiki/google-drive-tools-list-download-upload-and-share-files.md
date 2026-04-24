---
{
  "title": "Google Drive Tools: List, Download, Upload, and Share Files",
  "summary": "The `gdrive.py` module exposes four `BaseTool` subclasses — `DriveListTool`, `DriveDownloadTool`, `DriveUploadTool`, and `DriveShareTool` — giving the PocketPaw agent full CRUD and sharing access to Google Drive. A `_GDRIVE_ROLES` frozenset enforces valid sharing permissions at the tool layer before the API call is made, preventing invalid-role errors from propagating to the user.",
  "concepts": [
    "DriveListTool",
    "DriveDownloadTool",
    "DriveUploadTool",
    "DriveShareTool",
    "_GDRIVE_ROLES",
    "Google Drive API",
    "Drive query syntax",
    "trust level",
    "BaseTool",
    "frozenset validation"
  ],
  "categories": [
    "builtin tools",
    "Google Workspace",
    "cloud storage",
    "integrations"
  ],
  "source_docs": [
    "aeb1b59a02aa8f50"
  ],
  "backlinks": null,
  "word_count": 523,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`gdrive.py` was created 2026-02-09 as part of Phase 4 Media Integrations. Google Drive is the primary cloud storage surface for many business users, so providing full list/download/upload/share coverage enables a wide class of document and file management workflows.

All four tools carry `trust_level = "high"` because Drive access touches potentially sensitive files and requires explicit Google OAuth authorization.

## DriveListTool

Tool name: `drive_list`. Lists or searches files in the authenticated user's Drive using Drive's native query syntax (e.g., `"name contains 'report'"`, `"mimeType='application/pdf'"`). When no query is provided, it returns recently modified files. The description explicitly mentions Drive query syntax to guide the LLM toward correct usage — without this hint, the agent would likely pass natural-language queries that the Drive API cannot parse.

## DriveDownloadTool

Tool name: `drive_download`. Downloads a file from Drive by its file ID and saves it locally. Returning the local path in the response allows the agent to chain this with `ReadFileTool` or `OCRTool` in subsequent turns.

## DriveUploadTool

Tool name: `drive_upload`. Uploads a local file to Drive, optionally into a specific folder and with a custom display name. The `folder_id` parameter is optional — without it, the file goes to the Drive root. Accepting a `name` override prevents the Drive copy from having a generated filename like `tmpXXXXXX.csv`.

## DriveShareTool

Tool name: `drive_share`. Shares a Drive file with a specific user by email, assigning a role. The valid roles are constrained by `_GDRIVE_ROLES`:

```python
_GDRIVE_ROLES: frozenset[str] = frozenset({"reader", "writer", "commenter"})
```

This guard runs before the API call. Without it, passing an invalid role like `"owner"` or `"editor"` would reach the Drive API and return a cryptic HTTP 400 that the agent would have to interpret. Validating at the tool layer returns a clear error like `"Invalid role: editor. Must be one of reader, writer, commenter"` which the agent can explain directly to the user.

The `frozenset` type (rather than a `list` or `set`) signals immutability — these are not runtime-configurable values.

## Trust level and connector delegation

All tools are `trust_level = "high"`. The actual HTTP calls to the Google Drive API are delegated to a registered Google connector, keeping authentication and retry logic in one place and out of individual tool files.

## Drive query syntax guidance

The `DriveListTool` description includes query syntax examples inline:

```
"name contains 'report'", "mimeType='application/pdf'"
```

This is intentional prompt engineering in the tool description — Drive's query language is not natural language and the LLM will not invent the correct syntax without guidance. Including examples directly in the description reduces the chance of the agent passing a plain English query to a field that expects structured syntax.

## Known Gaps

- **No folder creation**: There is no `DriveCreateFolderTool`. The agent can upload files but cannot create the folder hierarchy they should live in.
- **No file delete or move**: Files can be uploaded and downloaded but not deleted, moved, or renamed after creation.
- **No Google Workspace file conversion**: Downloading a Google Sheets or Google Docs file returns the binary in a default export format (usually Office XML). There is no parameter to select the export MIME type.
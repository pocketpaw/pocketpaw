---
{
  "title": "Google Drive Client — OAuth-Backed HTTP Client for Drive API v3",
  "summary": "`DriveClient` provides async methods for listing, searching, downloading, uploading, and trashing files in Google Drive via the Drive API v3, using OAuth bearer tokens from `OAuthManager`. Downloaded files are saved to `~/.pocketpaw/downloads/` with automatic directory creation.",
  "concepts": [
    "Google Drive",
    "DriveClient",
    "Drive API v3",
    "OAuth",
    "list files",
    "download",
    "upload",
    "trash",
    "export",
    "multipart upload",
    "Google Workspace MIME types",
    "downloads directory"
  ],
  "categories": [
    "integrations",
    "Google Workspace"
  ],
  "source_docs": [
    "d14943e77c37be97"
  ],
  "backlinks": null,
  "word_count": 486,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`gdrive.py` implements `DriveClient`, PocketPaw's interface to the Google Drive API v3. It is the most feature-complete of the Google integration clients, covering the full CRUD lifecycle for Drive files.

## list_files — Search and Browse

```python
async def list_files(self, query: str | None = None, max_results: int = 20) -> list[dict]:
    params = {
        "pageSize": min(max_results, 100),
        "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink)",
        "orderBy": "modifiedTime desc",
    }
    if query:
        params["q"] = query
```

The `fields` parameter instructs the Drive API to return only the specified fields, reducing response size (and latency) compared to a full file object. `orderBy=modifiedTime desc` returns recently modified files first, which aligns with the typical "show me my recent files" use case.

The `query` parameter uses Drive's search syntax (e.g., `"name contains 'report' and mimeType='application/pdf'"`), enabling natural-language-driven file search when combined with an LLM that can translate user queries to Drive syntax.

## download — File Retrieval with Export Support

The `download` method handles two different Drive file types:

1. **Native files** (PDFs, images, etc.) — downloaded via `/files/{id}?alt=media`
2. **Google Workspace files** (Docs, Sheets, Slides) — exported via `/files/{id}/export?mimeType=...` because these formats cannot be directly downloaded as binary

```python
if mime_type.startswith("application/vnd.google-apps"):
    export_mime = _EXPORT_MIME_TYPES.get(mime_type, "text/plain")
    resp = await client.get(f"{_DRIVE_BASE}/files/{file_id}/export", params={"mimeType": export_mime})
else:
    resp = await client.get(f"{_DRIVE_BASE}/files/{file_id}", params={"alt": "media"})
```

The `_EXPORT_MIME_TYPES` mapping converts Google Workspace MIME types to export formats (e.g., Google Doc → DOCX, Google Sheet → XLSX). Files are saved to `~/.pocketpaw/downloads/` using a sanitized filename.

## upload — Multipart Upload

`upload()` uses the Drive multipart upload endpoint at `_UPLOAD_BASE`, sending both the file metadata (name, MIME type, optional parent folder ID) and the file content in a single request:

```python
files = {
    "metadata": (None, json.dumps({"name": name, "parents": [folder_id] if folder_id else []}), "application/json"),
    "file": (name, file_content, mime_type),
}
resp = await client.post(f"{_UPLOAD_BASE}/files?uploadType=multipart", files=files, headers=...)
```

The multipart upload is appropriate for files up to ~5 MB. For larger files, the Drive API requires a resumable upload session, which is not implemented here.

## trash — Soft Delete

Rather than permanently deleting files, `trash()` patches the file metadata with `{"trashed": true}`. This is safer than deletion — trashed files remain in the user's Drive Trash for 30 days and can be restored. An LLM agent that permanently deletes files on user request would be a significant data loss risk.

## Downloads Directory

```python
def _get_downloads_dir() -> Path:
    d = get_config_dir() / "downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d
```

The module-level helper creates the downloads directory on first use. `parents=True` ensures the full path is created even if `~/.pocketpaw/` does not yet exist.

## Known Gaps

- There is no resumable upload support — files larger than ~5 MB may fail or time out.
- Downloaded files are not deduplicated — downloading the same file twice creates duplicate local copies.
- The `trash()` method trashes by file ID, which requires the caller to know the ID. There is no `trash_by_name()` convenience method.

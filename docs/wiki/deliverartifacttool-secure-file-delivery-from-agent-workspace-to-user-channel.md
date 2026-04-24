---
{
  "title": "DeliverArtifactTool: Secure File Delivery from Agent Workspace to User Channel",
  "summary": "`DeliverArtifactTool` lets PocketPaw's agent send generated or downloaded files (images, PDFs, audio) to the user through their current channel — Slack, Discord, or the web dashboard — after verifying the file is within the agent's sandboxed working directory.",
  "concepts": [
    "DeliverArtifactTool",
    "file jail",
    "is_safe_path",
    "MIME type",
    "channel adapter",
    "artifact delivery",
    "security",
    "path traversal",
    "caption",
    "Slack",
    "Discord"
  ],
  "categories": [
    "tool-system",
    "security",
    "file-management",
    "channel-adapters"
  ],
  "source_docs": [
    "08e415c93df36603"
  ],
  "backlinks": null,
  "word_count": 408,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

When an agent generates a file (a report, a generated image, a processed audio clip), it needs a way to hand that file back to the user. `DeliverArtifactTool` is the mechanism: it reads a file from within the agent's file jail and routes it to the user via the active channel adapter.

## File Jail Enforcement

The most important security function in this tool is the jail check:

```python
jail = get_settings().file_jail_path.resolve()
if not is_safe_path(file_path, jail):
    return self._error(f"Access denied: {path} is outside allowed directory")
```

`is_safe_path` uses resolved absolute paths to verify that the requested file is inside `settings.file_jail_path`. Without this check, a prompt injection could instruct the agent to deliver `/etc/passwd` or `~/.ssh/id_rsa` to the user. The jail is typically set to a user-specific scratch directory (e.g., `~/.pocketpaw/files/<user_id>/`).

The check happens before any file I/O — the path is resolved and verified, then existence is checked, then the file type is validated (must be a regular file, not a directory or symlink to a system path).

## MIME Type Detection

Before routing the file to the channel adapter, `mimetypes.guess_type()` determines the file's MIME type from its extension. This is passed to the adapter so the channel can render the file appropriately — Slack and Discord display images inline, while arbitrary binary files are sent as downloads.

## Channel Routing

The tool does not directly upload to Slack or Discord. Instead, it calls into the channel adapter layer (via the message bus or a direct adapter reference from `get_settings()`) which handles the channel-specific upload API. This keeps the tool channel-agnostic: the same `deliver_artifact` call works across Slack, Discord, and the web dashboard.

## Caption Support

The optional `caption` parameter attaches a text message to the file delivery. In Slack, this becomes the message body accompanying the file upload. In the web dashboard, it becomes a caption shown below the file. This allows the agent to add context ("Here is the report you requested") alongside the file.

## Known Gaps

- **No size limit enforcement** — there is no check on file size before attempting delivery. A very large file (multiple GBs) would cause the channel adapter's upload to fail with a channel-specific error rather than a clean tool-level error.
- **Symlink traversal** — `is_safe_path` resolves symlinks via `Path.resolve()`, which should catch most symlink escapes, but the check only runs once. If a symlink is created between the check and the file open, a race condition exists.
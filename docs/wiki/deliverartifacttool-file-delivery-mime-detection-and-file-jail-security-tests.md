---
{
  "title": "DeliverArtifactTool: File Delivery, MIME Detection, and File Jail Security Tests",
  "summary": "This suite tests DeliverArtifactTool, which allows PocketPaw agents to deliver files (documents, images, videos) to user-facing channels by embedding them as media tags in the response. It covers basic delivery, optional captions, MIME type detection for images and video, error cases for missing files and directories, and the file jail security boundary that prevents agents from delivering arbitrary files from outside the allowed directory.",
  "concepts": [
    "DeliverArtifactTool",
    "file_jail",
    "MIME_detection",
    "media_tag",
    "caption",
    "file_not_found",
    "directory_rejection",
    "path_traversal",
    "file_size",
    "tool_definition",
    "get_settings",
    "Settings",
    "built_in_tools"
  ],
  "categories": [
    "testing",
    "tools",
    "security",
    "file-delivery",
    "agent-capabilities",
    "test"
  ],
  "source_docs": [
    "0f749a3824aead8b"
  ],
  "backlinks": null,
  "word_count": 562,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_deliver_artifact.py` tests `pocketpaw.tools.builtin.deliver.DeliverArtifactTool`, a built-in tool that enables agents to deliver files to the user as rich media attachments. When an agent generates an output file (a PDF report, a rendered chart, a video), this tool handles the final mile: validating the path, determining the MIME type, and emitting a structured media tag for the channel adapter to render.

## Why This Module Exists

Agents need a safe, controlled mechanism to deliver generated files. A naive approach (returning raw file paths) would expose the server filesystem to clients and allow agents to reference files outside their working area. `DeliverArtifactTool` provides a sandboxed delivery mechanism with explicit file-type awareness.

## Basic Delivery

`test_deliver_basic` confirms the core behavior: given an existing file path, `execute()` returns a string containing a `<!-- media:{path} -->` HTML comment tag and the filename. Channel adapters parse this tag to serve the file to the connected client. The comment format is used rather than raw HTML to avoid being rendered directly in Markdown contexts.

## Captions

`test_deliver_with_caption` verifies that an optional `caption` parameter is included in the result. Captions allow agents to add context to delivered files ("Here's your monthly report" alongside the PDF), improving the user experience in chat-based channels.

## MIME Type Detection

`test_deliver_image_mime` and `test_deliver_video_mime` verify that the tool correctly identifies file types from their extensions:

- `.jpg` files report `image/jpeg`.
- Video files report the correct video MIME type.

MIME detection matters because channel adapters use the type to render content appropriately — images can be inlined, videos need a player, documents need a download link. Incorrect MIME types result in files being rendered as raw downloads regardless of type.

## Error Cases

- `test_deliver_file_not_found`: A path that doesn't exist returns an error string rather than raising an exception. Tools in PocketPaw return errors as content so that agents can handle them gracefully.
- `test_deliver_not_a_file`: Passing a directory path returns an error. This prevents accidentally delivering directory listings.

## File Jail Security

`test_deliver_file_jail` is the security-critical test. The `file_jail_path` setting (read from `pocketpaw.config.Settings`) defines the root directory that agents are allowed to access. Any path outside this directory is rejected, even if the file exists.

This prevents a class of attacks where a compromised or misbehaving agent attempts to exfiltrate sensitive server files (SSH keys, environment files, credentials) by requesting delivery of paths like `/etc/passwd` or `~/.ssh/id_rsa`. The jail check uses path normalization to handle `../` traversal attempts.

The `mock_settings` fixture patches `get_settings` to point the jail at a temporary directory, ensuring tests run against a controlled, isolated directory rather than the real filesystem.

## File Size

`test_deliver_size_info` verifies that the result includes human-readable file size information. Users benefit from knowing a file is 2.3 MB before downloading, and channel adapters may use size to decide whether to inline or link the content.

## Tool Metadata

`test_deliver_definition` checks that the tool's definition (name, description, parameter schema) is correctly populated. This matters because the definition is exposed to the LLM as part of the tool manifest — incorrect metadata causes the LLM to generate malformed tool calls.

## Known Gaps

Tests use `tmp_path` as the jail root, but path traversal edge cases beyond basic `../` are not exhaustively tested. Delivery to specific channels (Discord vs. Slack vs. CLI) is not tested — the tool produces a generic media tag and channel adapters handle rendering differences.

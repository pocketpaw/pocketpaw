---
{
  "title": "ImageGenerateTool Unit Tests",
  "summary": "This test module verifies `ImageGenerateTool`, a built-in tool that generates images via the Google Gemini image generation API. It covers tool metadata (name, trust level, schema), graceful error handling for missing API keys and missing `google-genai` package, the generated directory creation logic, and the partial integration test structure for the image generation happy path.",
  "concepts": [
    "ImageGenerateTool",
    "image generation",
    "Google Gemini",
    "google-genai",
    "tool trust level",
    "parameter schema",
    "missing API key",
    "import mocking",
    "_get_generated_dir",
    "generated images",
    "built-in tools"
  ],
  "categories": [
    "testing",
    "image generation",
    "built-in tools",
    "error handling",
    "Google integration",
    "test"
  ],
  "source_docs": [
    "6c0fa1b823c6e8d5"
  ],
  "backlinks": null,
  "word_count": 460,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ImageGenerateTool` (`image_generate`) is a standard-trust built-in tool that calls Google's Gemini image generation API to produce images from text prompts. The tests in `test_image_gen.py` establish the behavioral contract for the tool's error paths and configuration requirements.

## Tool Metadata

- **Name**: `image_generate` — the string the agent uses to invoke the tool.
- **Trust level**: `standard` — requires standard user trust (not elevated), consistent with tools that produce output but do not execute system commands.
- **Parameter schema**: `prompt` (required), `aspect_ratio`, and `size`. Tests verify all three properties exist in the schema and that `prompt` is in the `required` list.

## Error Handling Tests

### Missing API Key

`test_missing_api_key` patches `get_settings` to return a settings object with `google_api_key=None`, then calls `tool.execute(prompt="a cat")`. The tool must return an error string containing "Error" and "Google API key". Without this check, the tool would pass `None` to the Google SDK and produce a cryptic authentication failure deep in the SDK stack.

### Missing `google-genai` Package

`test_missing_genai_package` simulates an environment where `google-genai` is not installed by patching `builtins.__import__` to raise `ImportError` for any import of `google` or `google.*`. The error message must reference `"google-genai"` so the user knows exactly which package to install. The Google SDK is an optional dependency — not all PocketPaw installations need image generation.

The import patching approach here is notably complex: it replaces `builtins.__import__` with a custom function that intercepts `google`-namespaced imports. This is necessary because `from google import genai` is resolved at call time inside `execute()`, not at module load time.

## Generated Directory

`test_generated_dir_creation` tests the `_get_generated_dir()` helper, which returns a `generated/` subdirectory under the PocketPaw config directory and creates it if absent. Without this check, a first-run image generation attempt would fail with a `FileNotFoundError` when trying to save the output.

## Happy Path and No-Images Tests

`test_image_generation_success` and `test_no_images_generated` set up deep mock chains for the Google genai client (`mock_genai.Client`, `mock_client.models.generate_images`, `mock_response.generated_images`). However, both tests acknowledge a limitation in their comments: the `from google import genai` import pattern inside `execute()` is difficult to intercept cleanly in the test process. Both tests end with a `pass` body rather than full assertions against the image output.

This is a known weak point — the tests establish the mock scaffolding but do not actually exercise the image-saving logic end-to-end.

## Known Gaps

The happy path tests are structurally incomplete: `test_image_generation_success` calls `mock_image.save.assert_not_called()` explicitly noting it did not run through the real path. No test verifies that the saved image file path is included in the tool's return string, or that `aspect_ratio` and `size` parameters are forwarded correctly to the API call. A TODO-equivalent comment (`"Since we can't easily mock `from google import genai`"`) appears inline, indicating the authors recognized the limitation but deferred a cleaner solution.
---
{
  "title": "ImageGenerateTool: Text-to-Image Generation via Google Gemini",
  "summary": "The `ImageGenerateTool` gives the PocketPaw agent the ability to generate images from text prompts using the Google Gemini image generation API (internally called \"Nano Banana\"). Generated images are saved to a deterministic directory under the PocketPaw config folder with UUID-based filenames, and the tool returns the local file path so the agent can chain the result with `OpenExplorerTool` or other file-handling tools.",
  "concepts": [
    "ImageGenerateTool",
    "_get_generated_dir",
    "Google Gemini",
    "text-to-image",
    "UUID filename",
    "config directory",
    "aspect ratio",
    "BaseTool",
    "trust level",
    "image generation"
  ],
  "categories": [
    "builtin tools",
    "media generation",
    "AI capabilities",
    "Google integrations"
  ],
  "source_docs": [
    "722442c60ccde8f2"
  ],
  "backlinks": null,
  "word_count": 581,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`image_gen.py` was created 2026-02-06 as part of Phase 1 Quick Wins. Image generation is a capability that users frequently request in creative and content workflows, and Google Gemini's image generation API is available to users who have configured a Gemini API key. The tool wraps that API in the standard `BaseTool` contract so it is available to any pocket.

## _get_generated_dir: deterministic output directory

```python
def _get_generated_dir() -> Path:
    """Get (and create) the directory for generated images."""
    d = get_config_dir() / "generated"
    d.mkdir(parents=True, exist_ok=True)
    return d
```

The generated images directory lives under `get_config_dir() / "generated"` rather than a temp directory or the current working directory. This choice matters for three reasons:

1. **Persistence**: Temp directories are cleared on reboot. A user may want to retrieve a generated image days later.
2. **Discoverability**: The `generated/` subdirectory under the PocketPaw config directory is a predictable location the agent can reference across sessions.
3. **`exist_ok=True`**: The directory is created on first call with `exist_ok=True` to avoid a race condition where two concurrent tool calls might both try to create the directory simultaneously.

## ImageGenerateTool

Tool name: `image_generate`. Trust level: `standard` (the default tier — it costs API credits but does not access user data). Parameters:

- `prompt` (required): Text description of the image.
- `aspect_ratio` (optional, default `"1:1"`): Supported values are `"1:1"`, `"16:9"`, `"9:16"`. This matches Gemini's accepted aspect ratio strings.
- `size` (optional, default `"1K"`): Output resolution hint passed to the API.

The aspect ratio enum is documented inline in the description to guide the LLM toward valid values. The Gemini API will reject unrecognized aspect ratios with an HTTP 400, so constraining the parameter at the schema level is preferable to handling API errors.

## UUID-based filenames

Generated images are saved with UUID-based filenames (e.g., `3f2a1b4c-....png`) rather than prompt-derived names. This prevents:

- Filename collisions when the same prompt is run twice
- Filesystem issues from special characters in prompt text (quotes, slashes, emoji)
- Predictable filenames that could be guessed by an attacker in a shared environment

## Return value and chaining

The tool returns the absolute path of the saved image file. This enables the agent to immediately chain with:

- `OpenExplorerTool` — to show the image in the file explorer
- `ReadFileTool` — in theory (though reading a binary image as text would fail)
- `DriveUploadTool` — to save the generated image to Google Drive

```python
async def execute(self, prompt: str, aspect_ratio: str = "1:1", size: str = "1K") -> str:
    # Call Gemini API, save image, return absolute path
```

## Configuration dependency

The tool reads `get_settings()` to check for the Gemini API key. If no key is configured, the tool returns an error rather than making the API call. This check happens inside `execute` rather than at registration time, so the tool is always visible in the agent's tool list even on unconfigured systems — which allows the agent to explain to the user what needs to be configured.

## Known Gaps

- **Single backend only**: The tool is hardcoded to Google Gemini. There is no fallback to DALL-E, Stable Diffusion, or other providers if Gemini is unavailable.
- **No image editing**: The tool only generates new images. Inpainting, outpainting, or style transfer on an existing image are not supported.
- **No prompt safety pre-check**: The prompt is passed directly to Gemini without a local content policy check. Gemini itself enforces safety filters, but the error messages from those filters may not be user-friendly.
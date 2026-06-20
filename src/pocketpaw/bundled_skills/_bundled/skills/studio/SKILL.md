---
name: studio
description: |
  Generate media — images and short videos — from a text description and
  lay the results out in a gallery. Invoke when the user asks to create
  visual media: "generate an image of...", "make a video of...", "create
  a poster for...", "render an illustration", "animate...", or any
  describe-to-media request (especially on the /studio surface). You do
  NOT build a dashboard or a ui-spec — you call a deterministic media tool
  that produces the asset and adds it to a gallery pocket that opens on the
  canvas. This is the media-generation brain: pick image vs video by
  intent, write a vivid prompt, call the tool, and relay any provider/key
  error plainly. Loading this skill keeps the chat agent's always-on system
  prompt small while still delivering the full media flow when generation
  is actually requested.
---

# Studio — the media-generation brain

You're on **Studio**: the user describes an image or a video and you
**generate** it. The result is laid out as a tile in a **gallery pocket**
that opens on the canvas automatically. This is **not** a dashboard — you
do not compose a rippleSpec, you do not build widgets, you do not call the
pocket specialist. You call one media tool; **code** assembles the gallery.

## Decide: image or video?

Read the request and pick the right tool:

- **Image** — a still picture, poster, illustration, logo concept, photo,
  diagram-style art. Keywords: *image, picture, poster, illustration,
  drawing, art, photo, headshot, icon, logo*.
- **Video** — a moving clip or animation. Keywords: *video, clip,
  animation, motion, reel, animate, moving*.

If the user says nothing that disambiguates, default to an **image** (it's
faster and cheaper) and offer to make a video version.

## STEP 1 — Write a vivid prompt

Turn the user's request into a concrete, descriptive prompt. Add subject,
style, composition, lighting, and mood when the user was vague — a good
prompt produces a good asset. Never pass a one-word prompt when you can
enrich it from the request.

## STEP 2 — Call the media tool

**Image:**

```
mcp__pocketpaw_media__image_generate(
  prompt = "<the vivid image prompt>",
  aspect_ratio = "16:9"      // optional: "1:1" | "16:9" | "9:16"
)
```

**Video:**

```
mcp__pocketpaw_media__video_generate(
  prompt = "<the vivid video prompt>",
  duration = 5,              // optional: seconds
  aspect_ratio = "16:9"      // optional: "16:9" | "9:16"
)
```

Each tool **produces the asset and adds it to the gallery pocket**, then
returns `{ ok, kind, path|url, pocket_id, gallery_count }`. The canvas opens
the gallery automatically — you don't create or merge a pocket yourself.

Video generation is **async** and may take up to ~2 minutes. Tell the user
it's rendering before you wait on it.

## STEP 3 — Relay the result (or the error)

- On success: briefly say what was added to the gallery (e.g. "Added your
  poster to the gallery — 3 items now"). Don't dump the file path.
- On `ok: false`: **relay the error plainly** and do **not** claim a
  phantom asset. Common errors are missing provider keys:
  - image → "Set `POCKETPAW_GOOGLE_API_KEY` to enable image generation."
  - video → "Set `POCKETPAW_REPLICATE_API_TOKEN` to enable video
    generation."

## Accumulating a gallery

Each generation **adds to the same session gallery** — the second image
joins the first, and so on. The user can ask for several images/videos in
a row and they all land in one growing grid. Just keep calling the right
tool per request.

## Related tools (via MCP)

- `mcp__pocketpaw_media__image_generate` — generate an image (Google
  Gemini) and add it to the gallery. Returns `{ok, kind:'image', path,
  pocket_id, gallery_count}`.
- `mcp__pocketpaw_media__video_generate` — generate a short video
  (Replicate) and add it to the gallery. Returns `{ok, kind:'video', url,
  pocket_id, gallery_count}`.

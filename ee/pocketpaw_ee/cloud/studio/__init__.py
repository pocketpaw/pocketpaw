# ee/pocketpaw_ee/cloud/studio — the /studio direct describe-to-media HTTP surface.
#
# The paw-enterprise /studio page drives its composer + gallery through a
# typed StudioBackend adapter. This package ships the real surface: a thin
# FastAPI router (``router``) over a service layer (``service``) that maps the
# LiteLLM proxy's model catalog to the Studio model list, runs image generation
# through the proxy's OpenAI-compatible ``/v1/images/generations`` endpoint (the
# same gateway the agent-side media MCP uses — fal.ai image models are already
# served), and persists a per-workspace generation history so the gallery
# survives reloads.
#
# Canvas EDIT ops (inpaint / expand-outpaint / upscale / variations / remove-bg
# / edit / sketch-to-image) are NOT proxy-able — LiteLLM has no route for fal's
# image-edit endpoints. They run directly against fal via ``fal_edit`` (the
# official fal-client SDK), and their outputs persist through the same media
# storage so the gallery + flow grow on every edit.
#
# The "Edit video" panel drives Kling Elements (``fal_elements``) directly
# against fal the same way — a source video + element/reference images + a
# prompt, dispatched through ``POST /studio/video-elements``. The "Motion
# control" panel drives Kling Motion Control (``fal_motion``) — a character image
# + reference motion video, dispatched through
# ``POST /studio/video-motion-control``.
#
# Wire shapes match paw-enterprise ``src/lib/core/studio/types.ts`` exactly
# (models/styles/generations/GenerateRequest/EditRequest/PromptSuggestion), so
# flipping the frontend's ``USE_MOCK`` flag is the only change needed there.

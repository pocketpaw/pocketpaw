# ee/pocketpaw_ee/cloud/studio — the /studio direct describe-to-media HTTP surface.
#
# The paw-enterprise /studio page drives its composer + gallery through a
# typed StudioBackend adapter. Until now it ran against an OFFLINE mock backend
# (SVG placeholder "images") because the real endpoints didn't exist. This
# package ships the real surface: a thin FastAPI router (``router``) over a
# service layer (``service``) that maps the LiteLLM proxy's model catalog to the
# Studio model list, runs image generation through the proxy's OpenAI-compatible
# ``/v1/images/generations`` endpoint (the same gateway the agent-side media MCP
# uses — fal.ai image models are already served), and persists a per-workspace
# generation history so the gallery survives reloads.
#
# Wire shapes match paw-enterprise ``src/lib/core/studio/types.ts`` exactly
# (models/styles/generations/GenerateRequest/EditRequest/PromptSuggestion), so
# flipping the frontend's ``USE_MOCK`` flag is the only change needed there.

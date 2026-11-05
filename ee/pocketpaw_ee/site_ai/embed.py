# ee/pocketpaw_ee/site_ai/embed.py — POST /sites/{site_id}/ai/embed.
#
# Site AI runs on the workspace's own agents (``cloud.chat.side_completion``), and
# an agent completion can't embed, so embeddings are not offered: owners and the
# builder agent are never told ``embed`` exists. The route stays so a site that
# still calls it gets a clear refusal instead of a 404: the same caller check as
# ``/ai/chat`` (the Worker token, or a browser / own-draft caller), then 503
# ``provider_unavailable`` with a message saying embeddings aren't available.
# Nothing is reserved or billed.

from __future__ import annotations

from fastapi import Request
from fastapi.responses import Response

from pocketpaw_ee.site_ai import chat
from pocketpaw_ee.site_ai.errors import AiError, error_response

UNAVAILABLE_MESSAGE = (
    "Embeddings aren't available for site AI. Use chat with a response_format instead, "
    "or keep retrieval in the site's own code."
)


async def handle(request: Request, site_id: str) -> Response:
    try:
        await chat.authenticate(request, site_id)
    except AiError as exc:
        return error_response(exc)
    return error_response(AiError("provider_unavailable", UNAVAILABLE_MESSAGE))

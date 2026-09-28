# ee/pocketpaw_ee/paw_bar/knowledge_routes.py — owner routes for a site
# concierge's knowledge sources.
#
# Created: 2026-09-28 (feat/concierge-pinned-faqs, CR-8) — pinned FAQs:
#   GET    /paw-bar/admin/site/{site_id}/knowledge/faqs            list + the caps
#   POST   /paw-bar/admin/site/{site_id}/knowledge/faqs            add one (201)
#   PATCH  /paw-bar/admin/site/{site_id}/knowledge/faqs/{faq_id}   edit question/answer
#   DELETE /paw-bar/admin/site/{site_id}/knowledge/faqs/{faq_id}   remove one (204)
# stored on ``Site.concierge_faqs``, which the v2 runner's ``retrieve`` puts ahead
# of every KB hit. Gated like the router's other owner routes: ``paw_bar.read`` on
# the GET and ``paw_bar.manage`` on the writes (both ADMIN), bound to the session's
# active workspace, and the site is loaded workspace-scoped, so a foreign or
# malformed id is one 404 and nothing is written. Caps come from config
# (``pawbar_concierge_faq_max_count`` / ``pawbar_concierge_faq_max_chars``): 409
# ``faq_limit_reached`` past the count, 422 ``faq_too_long`` past the length.
#
# ``delete_faqs(site)`` is the clear-all hook the concierge delete (CR-12) calls.
# Writes ``$set`` only ``concierge_faqs``, so a concurrent settings PATCH is not
# clobbered. Two concurrent adds can still land one past the count cap; the
# runner's knowledge budget bounds what that costs.
#
# A separate module so CR-9 (uploads and links, ``…/knowledge/sources``) extends
# this one instead of growing router.py. Mounted beside ``paw_bar.router`` in
# ``pocketpaw_ee.cloud``.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.cloud.models.site import ConciergeFaq
from pocketpaw_ee.paw_bar.router import (
    _load_site_scoped,
    _require_paw_bar_manage,
    _require_paw_bar_read,
)

router = APIRouter(tags=["PawBar"])

_FAQS = "/paw-bar/admin/site/{site_id}/knowledge/faqs"
# A body bound independent of config, so an absurd request is refused by the
# model before the configured cap is even read.
_HARD_MAX_CHARS = 10_000


def _settings() -> Any:
    """Indirection so tests can pin the caps without fighting get_settings' cache."""
    from pocketpaw.config import get_settings

    return get_settings()


def _caps() -> tuple[int, int]:
    settings = _settings()
    return (
        int(getattr(settings, "pawbar_concierge_faq_max_count", 15)),
        int(getattr(settings, "pawbar_concierge_faq_max_chars", 500)),
    )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("must not be blank")
    return value


class ConciergeFaqCreate(BaseModel):
    """Body of POST …/knowledge/faqs. Only the two texts: the id and timestamps
    are minted server-side."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(max_length=_HARD_MAX_CHARS)
    answer: str = Field(max_length=_HARD_MAX_CHARS)

    _strip = field_validator("question", "answer")(_clean)


class ConciergeFaqUpdate(BaseModel):
    """Body of PATCH …/knowledge/faqs/{faq_id}: either text, or both."""

    model_config = ConfigDict(extra="forbid")

    question: str | None = Field(default=None, max_length=_HARD_MAX_CHARS)
    answer: str | None = Field(default=None, max_length=_HARD_MAX_CHARS)

    _strip = field_validator("question", "answer")(_clean)


class ConciergeFaqListResponse(BaseModel):
    site_id: str
    faqs: list[ConciergeFaq]
    max_count: int
    max_chars: int


async def _load(site_id: str, workspace_id: str) -> Any:
    """The caller's own site, or a 404 (foreign, malformed and absent alike)."""
    return await _load_site_scoped(site_id, workspace_id)


def _check_length(question: str, answer: str, max_chars: int) -> None:
    if len(question) + len(answer) > max_chars:
        raise HTTPException(422, "faq_too_long")


async def _save_faqs(site: Any, faqs: list[ConciergeFaq]) -> None:
    site.concierge_faqs = faqs
    await site.set({"concierge_faqs": [f.model_dump() for f in faqs]})


def _find(site: Any, faq_id: str) -> int:
    for index, faq in enumerate(site.concierge_faqs):
        if faq.id == faq_id:
            return index
    raise HTTPException(404, "FAQ not found")


@router.get(
    _FAQS,
    response_model=ConciergeFaqListResponse,
    dependencies=[Depends(_require_paw_bar_read)],
)
async def list_site_faqs(
    site_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> ConciergeFaqListResponse:
    """The site's pinned FAQs in the order the concierge reads them, plus the caps
    so the dashboard can say how many more fit."""
    site = await _load(site_id, workspace_id)
    max_count, max_chars = _caps()
    return ConciergeFaqListResponse(
        site_id=str(site.id),
        faqs=list(site.concierge_faqs),
        max_count=max_count,
        max_chars=max_chars,
    )


@router.post(
    _FAQS,
    response_model=ConciergeFaq,
    status_code=201,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def add_site_faq(
    site_id: str,
    req: ConciergeFaqCreate,
    workspace_id: str = Depends(current_workspace_id),
) -> ConciergeFaq:
    """Pin a new answer, appended after the existing ones."""
    site = await _load(site_id, workspace_id)
    max_count, max_chars = _caps()
    if len(site.concierge_faqs) >= max_count:
        raise HTTPException(409, "faq_limit_reached")
    _check_length(req.question, req.answer, max_chars)
    faq = ConciergeFaq.new(req.question, req.answer)
    await _save_faqs(site, [*site.concierge_faqs, faq])
    return faq


@router.patch(
    _FAQS + "/{faq_id}",
    response_model=ConciergeFaq,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def update_site_faq(
    site_id: str,
    faq_id: str,
    req: ConciergeFaqUpdate,
    workspace_id: str = Depends(current_workspace_id),
) -> ConciergeFaq:
    """Edit a pinned answer's question and/or answer; the rest is kept."""
    site = await _load(site_id, workspace_id)
    index = _find(site, faq_id)
    current = site.concierge_faqs[index]
    question = req.question if req.question is not None else current.question
    answer = req.answer if req.answer is not None else current.answer
    _check_length(question, answer, _caps()[1])
    updated = current.model_copy(
        update={"question": question, "answer": answer, "updated_at": datetime.now(UTC)}
    )
    faqs = list(site.concierge_faqs)
    faqs[index] = updated
    await _save_faqs(site, faqs)
    return updated


@router.delete(
    _FAQS + "/{faq_id}",
    status_code=204,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def delete_site_faq(
    site_id: str,
    faq_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> Response:
    """Unpin one answer. The concierge stops using it on the next turn."""
    site = await _load(site_id, workspace_id)
    index = _find(site, faq_id)
    await _save_faqs(site, [f for i, f in enumerate(site.concierge_faqs) if i != index])
    return Response(status_code=204)


async def delete_faqs(site: Any) -> int:
    """Remove every pinned FAQ from ``site`` and return how many there were.

    The clear-all hook for the concierge delete (CR-12), beside CR-9's
    ``delete_sources(site)``. The caller has already loaded ``site`` for the
    caller's workspace; this does no tenancy check of its own. Idempotent.
    """
    count = len(getattr(site, "concierge_faqs", None) or [])
    if count:
        await _save_faqs(site, [])
    return count


__all__ = ["delete_faqs", "router"]

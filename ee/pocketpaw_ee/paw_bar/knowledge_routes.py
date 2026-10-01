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
#
# Updated: 2026-09-28 (feat/concierge-knowledge-sources, CR-9) — knowledge sources:
#   GET    /paw-bar/admin/site/{site_id}/knowledge/sources                     list + caps
#   POST   /paw-bar/admin/site/{site_id}/knowledge/sources                     add (202)
#   POST   /paw-bar/admin/site/{site_id}/knowledge/sources/{source_id}/refetch (202)
#   DELETE /paw-bar/admin/site/{site_id}/knowledge/sources/{source_id}         (204)
# The POST is multipart: a ``file`` (PDF, DOCX, Markdown, text) or a ``url`` field,
# exactly one. Refusals write nothing and use the row's status codes as the detail:
# 409 ``over_limit``, 413 ``too_large``, 415 ``unsupported``, 422 ``blocked``. An
# accepted source is stored as a ``processing`` row on ``Site.concierge_sources``
# and read into ``pocket:<pocket_id>`` in the background (``knowledge_sources``),
# then flips to ``ready`` or a refusal. The file's bytes are never stored: an
# upload is extracted and dropped, so there is no storage key to leak through the
# unauthenticated /uploads mount and nothing for the concierge delete to purge but
# the kb articles. A row still ``processing`` after ``_STALE_AFTER`` is reported
# as ``failed``/``interrupted`` (its task died with the process).
#
# Rows are written with $push, $pull and positional $set, never a whole-list save,
# so a background ingest finishing cannot clobber a concurrent add or delete. The
# count cap is part of the $push filter, so two concurrent adds cannot pass it. If
# the row was removed while its ingest ran, the article just written is deleted.
# Removing a source deletes its articles unless another row or the page sync
# (``kb_article_ids``) holds the same id: kb-go keys an article by its compiled
# title, so two sources can share one. ``delete_sources(site)`` is CR-12's hook.

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field, field_validator

from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.cloud.models.site import ConciergeFaq, ConciergeKnowledgeSource, Site
from pocketpaw_ee.paw_bar.knowledge_sources import (
    ACCEPTED_TYPES,
    SourceRefused,
    check_link,
    extract_file_text,
    fetch_link_text,
    missing_parser,
    sniff_upload,
)
from pocketpaw_ee.paw_bar.router import (
    _load_site_scoped,
    _require_paw_bar_manage,
    _require_paw_bar_read,
)

logger = logging.getLogger(__name__)

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


# --------------------------------------------------------------------------- #
# Knowledge sources (CR-9): uploaded files and single links
# --------------------------------------------------------------------------- #

_SOURCES = "/paw-bar/admin/site/{site_id}/knowledge/sources"
_STALE_AFTER = timedelta(minutes=15)
_HTTP_STATUS = {"over_limit": 409, "too_large": 413, "unsupported": 415, "blocked": 422}
_MAX_URL_CHARS = 2048
_MAX_NAME_CHARS = 200
_TASKS: set[asyncio.Task[Any]] = set()


def _schedule(coro: Any) -> None:
    """Run an ingest in the background and return at once. Tests patch this module
    attribute to run the coroutine when they choose."""
    task = asyncio.get_running_loop().create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def _plan_key(site: Any) -> str:
    """The site's own plan, canonical, or the free floor for none/unknown/org keys."""
    from pocketpaw_ee.cloud.billing.site_plans import BASE_SITE_PLAN_KEY, site_scoped_tier

    tier = site_scoped_tier(getattr(site, "plan_tier", None))
    return tier.key if tier is not None else BASE_SITE_PLAN_KEY


def _source_caps(site: Any) -> tuple[str, int, int, int]:
    """``(plan, max_count, max_bytes, max_chars)`` for this site, from config."""
    settings = _settings()
    plan = _plan_key(site)
    count_by_plan = {
        "free": settings.pawbar_concierge_source_max_count_free,
        "site": settings.pawbar_concierge_source_max_count_site,
        "staff": settings.pawbar_concierge_source_max_count_staff,
    }
    return (
        plan,
        int(count_by_plan.get(plan, count_by_plan["free"])),
        int(settings.pawbar_concierge_source_max_bytes),
        int(settings.pawbar_concierge_source_max_chars),
    )


class ConciergeSourceListResponse(BaseModel):
    site_id: str
    sources: list[ConciergeKnowledgeSource]
    plan: str
    max_count: int
    max_bytes: int
    max_chars: int
    accepted_types: list[str]


def _refuse(code: str) -> HTTPException:
    return HTTPException(_HTTP_STATUS[code], code)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _is_stale(row: ConciergeKnowledgeSource, now: datetime) -> bool:
    return row.status == "processing" and now - _aware(row.updated_at) > _STALE_AFTER


def _as_read(row: ConciergeKnowledgeSource, now: datetime) -> ConciergeKnowledgeSource:
    """A row as the owner should see it: one whose ingest died with its process
    reads as failed, so the dashboard offers a retry instead of polling forever."""
    if _is_stale(row, now):
        return row.model_copy(update={"status": "failed", "reason": "interrupted"})
    return row


def _find_source(site: Any, source_id: str) -> ConciergeKnowledgeSource:
    for row in getattr(site, "concierge_sources", None) or []:
        if row.id == source_id:
            return row
    raise HTTPException(404, "Source not found")


def _scope(site: Any) -> str:
    from pocketpaw_ee.sites.kb_ingest import kb_scope_for_pocket

    return kb_scope_for_pocket(site.pocket_id or "")


def _collection() -> Any:
    return Site.get_pymongo_collection()


async def _set_source(site_id: Any, source_id: str, fields: dict[str, Any]) -> bool:
    """Positional update of one row. False when the row (or the site) is gone."""
    update = {f"concierge_sources.$.{k}": v for k, v in fields.items()}
    result = await _collection().update_one(
        {"_id": site_id, "concierge_sources.id": source_id}, {"$set": update}
    )
    return result.matched_count > 0


async def _unindex(scope: str, article_ids: list[str], site_id: Any) -> int:
    """Delete ``article_ids`` from ``scope`` unless the site still holds one: in a
    remaining row, or among the page sync's ``kb_article_ids``."""
    from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService

    doc = await _collection().find_one(
        {"_id": site_id}, {"concierge_sources.article_ids": 1, "kb_article_ids": 1}
    )
    held: set[str] = set((doc or {}).get("kb_article_ids") or [])
    for row in (doc or {}).get("concierge_sources") or []:
        held.update(row.get("article_ids") or [])
    removed = 0
    for article_id in dict.fromkeys(article_ids):
        if article_id in held:
            continue
        if await KnowledgeService.remove_article(scope, article_id):
            removed += 1
    return removed


async def _read_source(
    kind: str, *, data: bytes | None, ext: str, mime: str, url: str | None, max_bytes: int
) -> tuple[str, str]:
    if kind == "link":
        return await fetch_link_text(url or "", max_bytes=max_bytes)
    return await extract_file_text(data or b"", ext, mime), mime


def _log_unreadable(refused: SourceRefused, *, kind: str, source_id: str, mime: str) -> None:
    """A missing parser is the deployment's fault and every upload of that type
    fails, so it logs at error; a file the parser cannot read logs at warning."""
    cause = refused.__cause__
    if missing_parser(cause):
        logger.error(
            "paw_bar.sources: no parser for %s source %s (%s): %s",
            kind,
            source_id,
            mime,
            cause,
            exc_info=refused,
        )
    else:
        logger.warning(
            "paw_bar.sources: %s source %s (%s) is unreadable: %s",
            kind,
            source_id,
            mime,
            cause,
            exc_info=refused,
        )


async def _ingest_source(
    site_id: Any,
    scope: str,
    source_id: str,
    *,
    kind: str,
    label: str,
    previous: list[str],
    max_bytes: int,
    max_chars: int,
    data: bytes | None = None,
    ext: str = "",
    mime: str = "",
    url: str | None = None,
) -> None:
    """Read one source into ``scope`` and record how it went on its row.

    Never raises: every outcome, including a bug, ends as a status on the row
    rather than a row stuck in ``processing``.
    """
    from pocketpaw_ee.cloud.agents.knowledge import (
        KnowledgeEngineUnavailable,
        KnowledgeService,
        extract_ingest_article_id,
    )

    try:
        text, mime = await _read_source(
            kind, data=data, ext=ext, mime=mime, url=url, max_bytes=max_bytes
        )
        text = text.strip()
        if not text:
            raise SourceRefused("failed", "no_content")
        truncated = len(text) > max_chars
        text = text[:max_chars]
        try:
            result = await KnowledgeService.ingest_text_to_scope(scope, text, label)
        except KnowledgeEngineUnavailable as exc:
            logger.warning(
                "paw_bar.sources: kb engine unavailable for %s source %s: %s",
                kind,
                source_id,
                exc,
                exc_info=True,
            )
            raise SourceRefused("failed", "kb_unavailable") from exc
        except Exception as exc:  # noqa: BLE001 — a compile failure is this source's
            logger.warning(
                "paw_bar.sources: ingest of %s source %s failed: %s",
                kind,
                source_id,
                exc,
                exc_info=True,
            )
            raise SourceRefused("failed", "ingest_failed") from exc
        article_id = extract_ingest_article_id(result)
        if not article_id:
            logger.warning(
                "paw_bar.sources: ingest of %s source %s returned no article id: %r",
                kind,
                source_id,
                result,
            )
            raise SourceRefused("failed", "ingest_failed")
    except SourceRefused as refused:
        if refused.reason == "unreadable":
            _log_unreadable(refused, kind=kind, source_id=source_id, mime=mime)
        fields = {"status": refused.status, "reason": refused.reason}
        await _set_source(site_id, source_id, {**fields, "updated_at": datetime.now(UTC)})
        return
    except Exception:  # noqa: BLE001 — never leave a row processing
        logger.warning("paw_bar.sources: ingest of %s crashed", source_id, exc_info=True)
        fields = {"status": "failed", "reason": "ingest_failed"}
        await _set_source(site_id, source_id, {**fields, "updated_at": datetime.now(UTC)})
        return

    now = datetime.now(UTC)
    kept = await _set_source(
        site_id,
        source_id,
        {
            "status": "ready",
            "reason": "",
            "mime": mime,
            "chars": len(text),
            "truncated": truncated,
            "article_ids": [article_id],
            "updated_at": now,
            "indexed_at": now,
        },
    )
    if not kept:
        # Removed while it was being read: the article just written is an orphan.
        await _unindex(scope, [article_id], site_id)
        return
    stale = [a for a in previous if a != article_id]
    if stale:
        await _unindex(scope, stale, site_id)


async def _read_upload(file: UploadFile, max_bytes: int) -> bytes:
    """The upload's bytes, refusing past ``max_bytes`` without reading further."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise _refuse("too_large")
        chunks.append(chunk)
    return b"".join(chunks)


async def _push_source(site: Any, row: ConciergeKnowledgeSource, max_count: int) -> None:
    """Append ``row`` only while the site holds fewer than ``max_count`` sources.
    The cap is in the filter, so concurrent adds cannot pass it together."""
    if max_count <= 0:
        raise _refuse("over_limit")
    result = await _collection().update_one(
        {"_id": site.id, f"concierge_sources.{max_count - 1}": {"$exists": False}},
        {"$push": {"concierge_sources": row.model_dump()}},
    )
    if result.matched_count == 0:
        raise _refuse("over_limit")


@router.get(
    _SOURCES,
    response_model=ConciergeSourceListResponse,
    dependencies=[Depends(_require_paw_bar_read)],
)
async def list_site_sources(
    site_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> ConciergeSourceListResponse:
    """The site's uploaded files and links with their status, plus the caps and the
    accepted file types so the dashboard can state them up front."""
    site = await _load(site_id, workspace_id)
    plan, max_count, max_bytes, max_chars = _source_caps(site)
    now = datetime.now(UTC)
    return ConciergeSourceListResponse(
        site_id=str(site.id),
        sources=[_as_read(row, now) for row in site.concierge_sources],
        plan=plan,
        max_count=max_count,
        max_bytes=max_bytes,
        max_chars=max_chars,
        accepted_types=list(ACCEPTED_TYPES),
    )


@router.post(
    _SOURCES,
    response_model=ConciergeKnowledgeSource,
    status_code=202,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def add_site_source(
    site_id: str,
    file: UploadFile | None = File(default=None),
    url: str | None = Form(default=None),
    workspace_id: str = Depends(current_workspace_id),
) -> ConciergeKnowledgeSource:
    """Add a file or one link. Returns the ``processing`` row; poll the list."""
    site = await _load(site_id, workspace_id)
    url = (url or "").strip() or None
    if (file is None) == (url is None):
        raise HTTPException(422, "one_source_required")
    _, max_count, max_bytes, max_chars = _source_caps(site)
    if len(site.concierge_sources) >= max_count:
        raise _refuse("over_limit")

    now = datetime.now(UTC)
    source_id = uuid.uuid4().hex
    data: bytes | None = None
    ext = mime = ""
    if file is not None:
        name = Path(file.filename or "").name[:_MAX_NAME_CHARS]
        data = await _read_upload(file, max_bytes)
        try:
            mime = sniff_upload(data, name, max_bytes=max_bytes)
        except SourceRefused as refused:
            raise _refuse(refused.status) from None
        ext = Path(name).suffix.lower()
        row = ConciergeKnowledgeSource(
            id=source_id,
            kind="file",
            name=name,
            mime=mime,
            size_bytes=len(data),
            created_at=now,
            updated_at=now,
        )
        label = f"upload:{name} ({source_id})"
    else:
        assert url is not None
        if len(url) > _MAX_URL_CHARS:
            raise HTTPException(422, "url_too_long")
        try:
            await check_link(url)
        except SourceRefused as refused:
            raise _refuse(refused.status) from None
        row = ConciergeKnowledgeSource(
            id=source_id, kind="link", name=url, url=url, created_at=now, updated_at=now
        )
        label = f"link:{url} ({source_id})"

    await _push_source(site, row, max_count)
    _schedule(
        _ingest_source(
            site.id,
            _scope(site),
            source_id,
            kind=row.kind,
            label=label,
            previous=[],
            max_bytes=max_bytes,
            max_chars=max_chars,
            data=data,
            ext=ext,
            mime=mime,
            url=url,
        )
    )
    return row


@router.post(
    _SOURCES + "/{source_id}/refetch",
    response_model=ConciergeKnowledgeSource,
    status_code=202,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def refetch_site_source(
    site_id: str,
    source_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> ConciergeKnowledgeSource:
    """Read a link again. Its old article is replaced once the new one lands."""
    site = await _load(site_id, workspace_id)
    row = _find_source(site, source_id)
    if row.kind != "link" or not row.url:
        raise HTTPException(409, "not_a_link")
    now = datetime.now(UTC)
    if row.status == "processing" and not _is_stale(row, now):
        raise HTTPException(409, "already_processing")
    try:
        await check_link(row.url)
    except SourceRefused as refused:
        raise _refuse(refused.status) from None
    _, _, max_bytes, max_chars = _source_caps(site)
    fields: dict[str, Any] = {"status": "processing", "reason": "", "updated_at": now}
    if not await _set_source(site.id, source_id, fields):
        raise HTTPException(404, "Source not found")
    _schedule(
        _ingest_source(
            site.id,
            _scope(site),
            source_id,
            kind="link",
            label=f"link:{row.url} ({source_id})",
            previous=list(row.article_ids),
            max_bytes=max_bytes,
            max_chars=max_chars,
            url=row.url,
        )
    )
    return row.model_copy(update=fields)


@router.delete(
    _SOURCES + "/{source_id}",
    status_code=204,
    dependencies=[Depends(_require_paw_bar_manage)],
)
async def delete_site_source(
    site_id: str,
    source_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> Response:
    """Remove a source and un-index it. The concierge stops using it on the next turn."""
    site = await _load(site_id, workspace_id)
    row = _find_source(site, source_id)
    await _collection().update_one(
        {"_id": site.id}, {"$pull": {"concierge_sources": {"id": source_id}}}
    )
    await _unindex(_scope(site), list(row.article_ids), site.id)
    return Response(status_code=204)


async def delete_sources(site: Any) -> int:
    """Remove every uploaded file and link from ``site``, un-index them, and return
    how many there were.

    The clear-all hook for the concierge delete (CR-12), beside ``delete_faqs``.
    The caller has already loaded ``site`` for the caller's workspace; this does no
    tenancy check of its own. Articles the page sync still lists in
    ``kb_article_ids`` are kept. An ingest still running finds its row gone and
    deletes its own article. Idempotent.
    """
    rows = list(getattr(site, "concierge_sources", None) or [])
    if not rows:
        return 0
    ids = [row.id for row in rows]
    await _collection().update_one(
        {"_id": site.id}, {"$pull": {"concierge_sources": {"id": {"$in": ids}}}}
    )
    site.concierge_sources = []
    await _unindex(_scope(site), [a for row in rows for a in row.article_ids], site.id)
    return len(rows)


__all__ = ["delete_faqs", "delete_sources", "router"]

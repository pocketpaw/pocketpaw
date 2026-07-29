# ee/pocketpaw_ee/cloud/growth/discovery.py — the ICP discovery engine: turn a
# standing description of who a workspace wants into prospects in its pipeline.
#
# Created 2026-07-29 (feat/growth-discovery): the run, the preview, and the
# injectable research seam. The cron and the workspace ceiling land alongside.
#
# THE SHAPE — copied deliberately from ``belt/headless.py``, which solved the
# same problem (a job that needs an LLM, and a test suite that must never call
# one):
#   * ``ResearchFn`` — an injectable async callable ``(ResearchRequest) ->
#     ResearchResult``. It is the genuine external boundary: the agent loop that
#     reads the criteria, searches, opens pages, and reports what it found.
#     Tests inject a deterministic fake that returns canned companies, so code
#     under test NEVER calls a real LLM. Production wires the real loop through
#     ``set_production_research_fn`` — an explicit follow-up, and until one is
#     wired ``resolve_research_fn`` returns None and the cron is a logged no-op
#     rather than a crash.
#   * ``run_discovery`` — research once, drop what cannot be filed, upsert the
#     rest. Never raises: a research failure is a zero-result run, not a dead
#     cron tick.
#   * ``preview_discovery`` — the same research, none of the writes.
#
# WHAT THIS MODULE MAY NOT DO, and why each is structural rather than a
# convention:
#   * It never INVENTS an email. Every address goes through
#     ``domain.recordable_emails``, which drops anything not observed on a page
#     we read. See that function for the reasoning; the short version is that a
#     guessed address bounces, the bounce lands on the sending domain's
#     reputation, and an LLM cannot run the verification waterfall that makes
#     Clay's addresses real. An empty ``emails`` is the honest result and a
#     perfectly good prospect.
#   * It never DRAFTS, never SENDS, and never touches ``gate_transition``. A
#     discovered prospect lands at ``status="new"`` with no draft attached — the
#     same place a pasted domain lands. Everything downstream of that still runs
#     through the human gate it always did. Discovery adds rows to a list; it
#     does not start conversations.
#   * It never writes through a second path. Every row is filed by
#     ``service.upsert_by_domain``, the same seam bulk import and the agent
#     surface use, so the (workspace, domain) dedupe key holds across all of
#     them and there is one place where a prospect comes into existence.
#   * It never re-files a domain the workspace already has (see
#     ``service.prospect_exists_by_domain``): a daily cron that re-upserts a
#     live prospect would reset its ``status`` and lose the follow-up thread.
#     Discovery only ever inserts.

"""ICP discovery for the /growth outbound engine."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from pocketpaw_ee.cloud._core.errors import CloudError, RateLimited
from pocketpaw_ee.cloud.growth.domain import (
    GROWTH_DISCOVERY_SWEEP_JOB_NAME,
    SCHEDULED_CADENCES,
    WEEKLY_DISCOVERY_WEEKDAY,
    EmailEvidence,
    recordable_emails,
)

logger = logging.getLogger(__name__)

# How many prospects ONE workspace may have discovered in a calendar month,
# across every ICP it owns. Read from the environment at CALL time (not
# import) so a redeploy-free change and the tests' monkeypatching both work —
# same convention as the follow-up sweep's knobs.
#
# The default is deliberately a number a human could actually work through.
# The failure mode this bounds is not cost, it is a pipeline nobody reviews:
# an ICP left running against loose criteria fills the list with companies
# nobody chose, and a list nobody trusts stops getting opened at all.
DEFAULT_DISCOVERY_MONTHLY_MAX = 200

# How many due ICPs one sweep tick will work through.
DUE_ICP_SCAN_LIMIT = 500


def discovery_monthly_max() -> int:
    """The per-workspace monthly discovery ceiling. Fail-soft on a bad value."""
    raw = os.environ.get("GROWTH_DISCOVERY_MONTHLY_MAX", "").strip()
    if not raw:
        return DEFAULT_DISCOVERY_MONTHLY_MAX
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "growth discovery: GROWTH_DISCOVERY_MONTHLY_MAX=%r is not an integer — using %d",
            raw,
            DEFAULT_DISCOVERY_MONTHLY_MAX,
        )
        return DEFAULT_DISCOVERY_MONTHLY_MAX
    # 0 disables discovery entirely, which is a legitimate thing to want on a
    # deployment that hasn't reviewed the feature yet. Negative is a typo.
    return max(value, 0)


def _period_start(now: datetime) -> datetime:
    """The start of the calendar month ``now`` falls in, in UTC.

    A calendar month rather than a rolling 30-day window: an operator asked
    "how many did we find this month" answers from a calendar, and a ceiling
    they cannot compute in their head is a ceiling they will be surprised by.
    """
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# The research boundary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResearchRequest:
    """Everything the research loop needs for ONE run of one ICP.

    Flattened out of the ICP rather than passing the ICP itself: the research
    loop is the external boundary, and handing it a workspace-scoped domain
    object would invite it to reach for fields (ids, cadence, timestamps) that
    are none of its business. ``max_results`` is the ICP's ``max_per_run`` — a
    request, not a guarantee; the caller truncates whatever comes back.
    """

    workspace_id: str
    icp_id: str
    criteria: str
    icp_name: str = ""
    geography: str = ""
    exclusions: str = ""
    max_results: int = 10
    project_id: str | None = None


@dataclass(frozen=True)
class DiscoveredCompany:
    """One company the research found.

    ``domain`` is the only field that matters — it is the dedupe identity, and
    a result without one is not a prospect (a company nobody can look up is not
    a lead). Everything else may legitimately be empty: a bare domain plus the
    pages it was found on is a perfectly good row for a human to qualify.

    ``emails`` carries EVIDENCE, not addresses. The research reports what it
    saw and where; ``recordable_emails`` decides what may be stored. That split
    is the whole safety property — see the module header.
    """

    domain: str
    name: str = ""
    company: str = ""
    research_brief: str = ""
    source_urls: tuple[str, ...] = ()
    emails: tuple[EmailEvidence, ...] = ()


@dataclass(frozen=True)
class ResearchResult:
    """What one research pass returned. ``notes`` is free text for the run log
    ("searched 4 directories, 2 were paywalled") — it is never stored on a
    prospect, so it cannot become an unattributed claim about a company."""

    companies: tuple[DiscoveredCompany, ...] = ()
    notes: str = ""


class ResearchFn(Protocol):
    """Injectable research loop — the genuine external boundary (the agent run
    that turns criteria into companies). Tests inject a deterministic fake;
    production wires the real loop. Async so the real implementation can await
    an agent."""

    async def __call__(self, request: ResearchRequest) -> ResearchResult: ...


# ---------------------------------------------------------------------------
# Run outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreviewedProspect:
    """One company as it WOULD be filed — the preview row.

    ``emails`` here has already been through the observed-only filter, so what
    the preview shows is exactly what a run would store. A preview that
    displayed the raw evidence would be advertising addresses the engine
    refuses to keep.
    """

    domain: str
    name: str = ""
    company: str = ""
    research_brief: str = ""
    source_urls: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()
    # Already in the pipeline — a run would SKIP this one. Surfaced rather than
    # hidden so a preview full of them says "your criteria describe people you
    # already have", which is the useful thing to learn before switching a
    # cadence on.
    already_known: bool = False


@dataclass(frozen=True)
class DiscoveryOutcome:
    """What one run did. Counts rather than rows: the caller that wants the
    rows reads the prospect list, which is where they now are."""

    icp_id: str
    workspace_id: str
    filed: int = 0
    skipped_existing: int = 0
    skipped_invalid: int = 0
    considered: int = 0
    notes: str = ""
    # Why a run produced nothing, when it produced nothing for a reason worth
    # reporting (research crashed, the ICP is paused, the workspace is at its
    # monthly ceiling). Empty on a clean run — including a clean run that found
    # no companies, which is an answer rather than a fault.
    error: str = ""


# ---------------------------------------------------------------------------
# Shared internals
# ---------------------------------------------------------------------------


async def _research(
    icp: object,
    research_fn: ResearchFn,
    *,
    workspace_id: str,
) -> tuple[ResearchResult | None, str]:
    """Run the research once. Returns ``(result, error)`` — never raises.

    A research failure is a run that found nothing, not a dead cron tick: the
    next tick tries again, and one bad ICP must not take the pass down for
    every other workspace. Same discipline as ``belt/headless``'s develop loop.
    """
    request = ResearchRequest(
        workspace_id=workspace_id,
        icp_id=str(getattr(icp, "id", "")),
        icp_name=str(getattr(icp, "name", "")),
        criteria=str(getattr(icp, "criteria", "")),
        geography=str(getattr(icp, "geography", "")),
        exclusions=str(getattr(icp, "exclusions", "")),
        max_results=int(getattr(icp, "max_per_run", 10)),
        project_id=getattr(icp, "project_id", None),
    )
    try:
        result = await research_fn(request)
    except Exception as exc:  # noqa: BLE001 — research must not crash the run
        logger.warning(
            "growth discovery: research failed for icp=%s workspace=%s — filing nothing",
            request.icp_id,
            workspace_id,
            exc_info=True,
        )
        return None, f"research failed: {exc}"
    if result is None:
        return ResearchResult(), ""
    return result, ""


def _clean_urls(urls: tuple[str, ...]) -> list[str]:
    """Trim, drop blanks, collapse duplicates, and cap the trail.

    The cap is not paranoia about storage: a source list long enough to need
    scrolling is one nobody reads, and an audit trail nobody reads is not one.
    """
    seen: dict[str, None] = {}
    for url in urls:
        cleaned = url.strip()
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)[:20]


def _to_preview(company: DiscoveredCompany, *, already_known: bool) -> PreviewedProspect:
    """Project one research result onto the row it would become.

    THE single place evidence turns into addresses, for both the run and the
    preview — so the preview cannot drift from what the run actually files, and
    there is exactly one call to ``recordable_emails`` to audit.
    """
    return PreviewedProspect(
        domain=company.domain.strip().lower(),
        name=company.name.strip(),
        company=company.company.strip(),
        research_brief=company.research_brief.strip(),
        source_urls=tuple(_clean_urls(company.source_urls)),
        emails=recordable_emails(company.emails),
        already_known=already_known,
    )


# ---------------------------------------------------------------------------
# The preview — the same research, none of the writes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveryPreview:
    """What a run WOULD file, and why it might file less than it found."""

    icp_id: str
    workspace_id: str
    items: tuple[PreviewedProspect, ...] = ()
    notes: str = ""
    error: str = ""


async def preview_discovery(
    workspace_id: str,
    icp_id: str,
    research_fn: ResearchFn,
) -> DiscoveryPreview:
    """Run the research once and report what WOULD be filed. Writes nothing.

    This is how someone comes to trust an ICP before switching a cadence on.
    Criteria are prose, and prose that reads precisely to its author routinely
    describes the wrong companies — the only way to find that out is to look at
    the rows it produces. Doing that by turning a cadence on and reading the
    pipeline afterwards means cleaning up a list you did not want.

    The projection is the SAME ``_to_preview`` the run uses, so the preview
    cannot drift from what a run actually stores — including the email rule.
    Showing raw evidence here would advertise addresses the engine refuses to
    keep, which is exactly the confusion the rule exists to prevent.

    A paused ICP still previews: checking whether a profile is worth resuming
    is the reason to look at one.
    """
    from pocketpaw_ee.cloud.growth import service as growth_service

    icp = await growth_service.get_icp_system(workspace_id, icp_id)
    result, error = await _research(icp, research_fn, workspace_id=workspace_id)
    if result is None:
        return DiscoveryPreview(icp_id=icp_id, workspace_id=workspace_id, error=error)

    items: list[PreviewedProspect] = []
    for company in result.companies[: max(icp.max_per_run, 0)]:
        preview = _to_preview(company, already_known=False)
        if not preview.domain:
            continue
        known = await growth_service.prospect_exists_by_domain(workspace_id, preview.domain)
        items.append(_to_preview(company, already_known=known))

    return DiscoveryPreview(
        icp_id=icp_id,
        workspace_id=workspace_id,
        items=tuple(items),
        notes=result.notes,
    )


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


async def run_discovery(
    workspace_id: str,
    icp_id: str,
    research_fn: ResearchFn,
    *,
    now: datetime | None = None,
) -> DiscoveryOutcome:
    """Run one ICP and file what it found.

    The sequence is deliberately boring: read the ICP, check the workspace's
    monthly room, research once, truncate to what may still be filed, drop the
    rows that cannot be filed, upsert the rest at ``status="new"``. Nothing
    here drafts, sends, or touches the Instinct gate — a discovered prospect
    arrives in exactly the state a pasted domain does, and every outbound step
    downstream still needs the human it always needed.

    TWO bounds, and they do different jobs. ``max_per_run`` is per ICP and
    caps one batch at a size a human will actually read. The monthly ceiling is
    per WORKSPACE and caps automated volume however many ICPs it is spread
    across — five profiles must not buy five allowances. When the month has
    less room left than this run wants, the run is TRUNCATED to the room
    available; only a workspace with zero room left is refused outright, with a
    429 ``icp.monthly_ceiling`` (a volume limit, not a wallet problem — see
    ``RateLimited``).

    Rows skipped because the workspace already has them do not free budget for
    extra ones: the number a human agreed to review is the number that can
    appear.

    Raises only ``NotFound`` (the ICP is gone) and the ceiling's
    ``RateLimited``. Everything else — a research crash, an unfilable row — is
    a counted non-event, because one bad ICP must not end a cron pass that has
    other workspaces left to serve.

    A paused ICP returns a zero outcome rather than raising: the cron already
    filters on status, and a manual caller asking for a paused profile wants
    "it didn't run", not a 4xx.
    """
    from pocketpaw_ee.cloud.growth import service as growth_service
    from pocketpaw_ee.cloud.growth.dto import CreateProspectRequest

    now = now or datetime.now(UTC)
    icp = await growth_service.get_icp_system(workspace_id, icp_id)
    if icp.status != "active":
        logger.info("growth discovery: icp=%s is %s — not running", icp_id, icp.status)
        return DiscoveryOutcome(
            icp_id=icp_id, workspace_id=workspace_id, error=f"icp is {icp.status}"
        )

    # The ceiling is checked BEFORE the research pass, not after: a workspace
    # that is out of room should not spend an agent run to discover that.
    ceiling = discovery_monthly_max()
    spent = await growth_service.count_discovered_since(workspace_id, _period_start(now))
    room = max(ceiling - spent, 0)
    if room <= 0:
        raise RateLimited(
            "icp.monthly_ceiling",
            f"This workspace has discovered {spent} prospects this month, "
            f"which is its ceiling of {ceiling}. Discovery resumes next month; "
            f"pause an ICP or narrow its criteria if this is not what you wanted.",
        )

    result, error = await _research(icp, research_fn, workspace_id=workspace_id)
    if result is None:
        return DiscoveryOutcome(icp_id=icp_id, workspace_id=workspace_id, error=error)

    considered = result.companies[: max(min(icp.max_per_run, room), 0)]
    filed = 0
    skipped_existing = 0
    skipped_invalid = 0

    for company in considered:
        preview = _to_preview(company, already_known=False)
        if not preview.domain:
            # A result with no domain is not a prospect — there is nothing to
            # dedupe on and nothing for a human to open.
            skipped_invalid += 1
            continue
        if await growth_service.prospect_exists_by_domain(workspace_id, preview.domain):
            skipped_existing += 1
            continue

        try:
            await growth_service.upsert_by_domain(
                workspace_id,
                CreateProspectRequest(
                    name=preview.name,
                    company=preview.company,
                    domain=preview.domain,
                    source="discovery",
                    project_id=icp.project_id,
                    research_brief=preview.research_brief,
                    # ALREADY filtered to observed-on-a-page addresses. This is
                    # the only place a discovered prospect's emails are set,
                    # and the value came from ``recordable_emails``.
                    emails=list(preview.emails),
                    status="new",
                    icp_id=icp_id,
                    source_urls=list(preview.source_urls),
                ),
            )
        except Exception:  # noqa: BLE001 — one bad row must not end the run
            logger.warning(
                "growth discovery: could not file %s for icp=%s — skipping",
                preview.domain,
                icp_id,
                exc_info=True,
            )
            skipped_invalid += 1
            continue
        filed += 1

    logger.info(
        "growth discovery: icp=%s workspace=%s considered=%d filed=%d "
        "skipped_existing=%d skipped_invalid=%d",
        icp_id,
        workspace_id,
        len(considered),
        filed,
        skipped_existing,
        skipped_invalid,
    )
    return DiscoveryOutcome(
        icp_id=icp_id,
        workspace_id=workspace_id,
        filed=filed,
        skipped_existing=skipped_existing,
        skipped_invalid=skipped_invalid,
        considered=len(considered),
        notes=result.notes,
    )


# ---------------------------------------------------------------------------
# The cron sweep
# ---------------------------------------------------------------------------


def _is_due(cadence: str, now: datetime) -> bool:
    """Is this cadence due on this tick?

    ``daily`` on every tick; ``weekly`` only on ``WEEKLY_DISCOVERY_WEEKDAY``.
    The weekday is fixed rather than per-ICP because "weekly" has to mean a
    predictable day — otherwise an operator cannot tell a skipped run from a
    run that was never due.

    Note what is NOT here: no comparison against ``last_run_at``. A worker that
    was down for three days resumes with ONE normal run rather than a backlog
    that fires all at once, which is the behaviour you want from something that
    spends agent time and fills a list a human has to read.
    """
    if cadence == "daily":
        return True
    if cadence == "weekly":
        return now.astimezone(UTC).weekday() == WEEKLY_DISCOVERY_WEEKDAY
    return False


async def discovery_sweep(
    ctx: dict[str, Any], *, now: datetime | None = None
) -> dict[str, int]:
    """Run every due ICP across every tenant. NEVER raises.

    Registered as a daily arq cron on the ``growth`` queue. ``ctx`` is arq's
    job context (unused — the sweep resolves everything itself); ``now`` is the
    injectable clock the tests freeze instead of sleeping, exactly as the
    follow-up sweep does it.

    Returns a counter dict for the worker log: how many ICPs were due, how many
    ran, how many prospects were filed, and how many ICPs were skipped (paused
    between the scan and the run, out of monthly room, or their research
    failed). One failing ICP is a skip, never the end of the pass — the next
    workspace in the list has done nothing wrong.

    With no production ``ResearchFn`` wired the sweep logs once and does
    nothing. That is the correct degraded state: an ICP with a cadence on and
    no research behind it should be visibly idle, never a source of
    half-researched rows.
    """
    from pocketpaw_ee.cloud.growth import service as growth_service

    now = (now or datetime.now(UTC)).astimezone(UTC)
    counters = {"due": 0, "ran": 0, "filed": 0, "skipped": 0}

    research_fn = resolve_research_fn()
    if research_fn is None:
        logger.info(
            "growth discovery: no research loop is wired (see "
            "discovery.set_production_research_fn) — sweep is a no-op"
        )
        return counters

    try:
        icps = await growth_service.list_due_icps(
            sorted(SCHEDULED_CADENCES), limit=DUE_ICP_SCAN_LIMIT
        )
    except Exception:  # noqa: BLE001 — a failed scan is a no-op pass, not a crash
        logger.exception("growth discovery: could not read the due ICPs")
        return counters

    for icp in icps:
        if not _is_due(icp.cadence, now):
            continue
        counters["due"] += 1
        try:
            outcome = await run_discovery(icp.workspace_id, icp.id, research_fn, now=now)
        except CloudError as exc:
            # The monthly ceiling, or an ICP deleted between the scan and the
            # run. Both are ordinary — log the code and move to the next one.
            counters["skipped"] += 1
            logger.info(
                "growth discovery: skipped icp=%s workspace=%s — %s",
                icp.id,
                icp.workspace_id,
                exc.code,
            )
            continue
        except Exception:  # noqa: BLE001 — one bad ICP must not end the pass
            counters["skipped"] += 1
            logger.exception(
                "growth discovery: icp=%s workspace=%s failed", icp.id, icp.workspace_id
            )
            continue

        if outcome.error:
            counters["skipped"] += 1
            continue
        counters["ran"] += 1
        counters["filed"] += outcome.filed
        try:
            await growth_service.mark_icp_run(icp.workspace_id, icp.id, now)
        except Exception:  # noqa: BLE001 — a missing timestamp is not a failed run
            logger.warning(
                "growth discovery: could not stamp last_run_at for icp=%s", icp.id, exc_info=True
            )

    logger.info(
        "growth discovery sweep: due=%d ran=%d filed=%d skipped=%d",
        counters["due"],
        counters["ran"],
        counters["filed"],
        counters["skipped"],
    )
    return counters


# ---------------------------------------------------------------------------
# Production wiring seam — the research loop.
# ---------------------------------------------------------------------------
#
# The real agent loop is a follow-up; this hook lets it be wired without
# touching the cron again. Until one is wired, ``resolve_research_fn`` returns
# None and the discovery sweep is a logged no-op — a deploy is never left with
# a cron that crashes on every tick, and code under test NEVER reaches a real
# LLM (tests pass a fake ``ResearchFn`` straight into ``run_discovery`` /
# ``preview_discovery``, bypassing this seam entirely).

_PRODUCTION_RESEARCH_FN: ResearchFn | None = None


def set_production_research_fn(fn: ResearchFn | None) -> None:
    """Wire (or clear) the production research loop the discovery cron uses.

    Called once at app wiring time by a deploy that ships a real loop. Tests do
    NOT use this — they inject a fake directly."""
    global _PRODUCTION_RESEARCH_FN
    _PRODUCTION_RESEARCH_FN = fn


def resolve_research_fn() -> ResearchFn | None:
    """The wired production research loop, or None.

    None means the cron logs and does nothing. That is the correct degraded
    state: an ICP with a cadence on and no research loop behind it should be
    visibly idle, never a source of half-researched rows."""
    return _PRODUCTION_RESEARCH_FN


__all__ = [
    "DEFAULT_DISCOVERY_MONTHLY_MAX",
    "GROWTH_DISCOVERY_SWEEP_JOB_NAME",
    "DiscoveredCompany",
    "DiscoveryOutcome",
    "DiscoveryPreview",
    "PreviewedProspect",
    "ResearchFn",
    "ResearchRequest",
    "ResearchResult",
    "discovery_monthly_max",
    "discovery_sweep",
    "preview_discovery",
    "resolve_research_fn",
    "run_discovery",
    "set_production_research_fn",
]

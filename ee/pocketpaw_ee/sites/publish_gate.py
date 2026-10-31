# ee/pocketpaw_ee/sites/publish_gate.py — "make sure it works before it goes live".
#
# A live publish (``service.publish_pocket``, REST and the agent's publish tool) is
# refused with :class:`PublishBlocked` (409 ``sites.publish_blocked``) while the draft
# still has a BLOCKING page finding: a form that saves nothing (SC-LINK-04), placeholder
# copy such as "[Address]" (SC-HONEST-07), a made-up phone number (SC-HONEST-03), or a
# form that failed when ``play_test_site`` submitted test data to it (SC-FORM-01). The
# owner can still take it live on purpose (``confirm_placeholders``, audited).
#
# The approval path runs it too: ``service.request_publish_pocket`` stores the findings
# on the review card (``publish_findings``), and ``versions.instinct_executor`` checks
# again BEFORE it merges, failing the Action with them unless that one approve said
# ``confirm_placeholders``.
#
# WHAT IT READS IS WHAT GOES LIVE. ``verify.publish_checks`` reads the html or no-build
# source, or the stored build of the current content (a project publish deploys exactly
# that bundle and refuses without it), plus the stored form-exercise record. A svelte /
# react draft with no stored build is checked on the build the publish makes, right
# before the deploy (:func:`check_built_output`); on the async build lane that check
# runs in the worker's deploy, and a refusal there is kept for the build status
# (:func:`record_blocked`). Only a checker CRASH lets a publish through, logged with
# :data:`FAIL_OPEN_METRIC`.
#
# WHO MAY OVERRIDE. Only the owner: ``confirm_placeholders`` from the REST publish or an
# individual Tray approve, written to the audit log with the findings
# (:func:`audit_override`). The agent's publish tool files a Tray card instead. A
# machine republish is exempt only for content that last passed or was confirmed
# (:func:`gate_publish`, the hash in ``verify_store``'s ``publish-gate`` pointer).
#
# Every finding is worded for the site owner ("Your site still shows [Address] on the
# home page"); the error shape is documented in docs/api-reference.md for the client.
from __future__ import annotations

import asyncio
import logging
import posixpath
import time
from collections.abc import Iterable, Mapping
from typing import Any

from pocketpaw_ee.cloud._core.errors import CloudError

logger = logging.getLogger(__name__)

#: Findings carried on one refusal; the rest are counted in ``details.more``.
MAX_FINDINGS = 20
#: The publish field (REST body, MCP arg, service kwarg) that skips the gate.
OVERRIDE_FIELD = "confirm_placeholders"
#: The rule id a failed form exercise is recorded under.
FORM_RULE = "SC-FORM-01"

_REASONS = {
    "SC-HONEST-07": "placeholder",
    "SC-HONEST-03": "invented_contact",
    "SC-LINK-04": "form_saves_nothing",
    FORM_RULE: "form_failed",
}


class PublishBlocked(CloudError):
    """409: the draft still shows placeholders, made-up contacts or broken forms."""

    def __init__(self, findings: list[dict[str, Any]], more: int = 0) -> None:
        first = findings[0]["message"] if findings else "Your site has things to fix."
        extra = len(findings) + more - 1
        tail = f" (and {extra} more {'thing' if extra == 1 else 'things'} to fix)" if extra else ""
        super().__init__(
            409,
            "sites.publish_blocked",
            f"Your site isn't ready to go live yet. {first}{tail} Fix these, or publish "
            "anyway if you meant to.",
            details={"findings": findings, "more": more, "override": OVERRIDE_FIELD},
        )


def page_label(page: str) -> str:
    """``index.html`` → ``the home page``; ``about/index.html`` → ``the "about" page``."""
    path = page.strip("/")
    if path.endswith("index.html"):
        path = posixpath.dirname(path)
    else:
        path = posixpath.splitext(path)[0]
    if not path:
        return "the home page"
    return f'the "{path.replace("/", " / ").replace("-", " ")}" page'


def _message(rule: str, text: str, where: str, outcome: str) -> str:
    if rule == "SC-HONEST-07":
        return f'Your site still shows "{text}" on {where}.'
    if rule == "SC-HONEST-03":
        return (
            f'{where[0].upper()}{where[1:]} shows the number "{text}", which looks made up '
            "and could belong to a stranger. Add your real number or remove it."
        )
    if rule == "SC-LINK-04":
        return f'The "{text}" form on {where} doesn\'t save what visitors type.'
    return f'The "{text}" form on {where} didn\'t work when we tried it{outcome}.'


def owner_findings(entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One owner-worded finding per item of each blocking entry:
    ``{rule, reason, page, page_label, text, message}``."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in entries:
        rule = str(entry.get("id") or "")
        page = str(entry.get("page") or "")
        where = page_label(page)
        if entry.get("pages", 1) > 1:
            where += f" and {entry['pages'] - 1} other page{'s' if entry['pages'] > 2 else ''}"
        outcome = f" ({entry['outcome']})" if entry.get("outcome") else ""
        for text in entry.get("items") or [""]:
            key = (rule, page, str(text))
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "rule": rule,
                    "reason": _REASONS.get(rule, "blocked"),
                    "page": page,
                    "page_label": page_label(page),
                    "text": str(text),
                    "message": _message(rule, str(text), where, outcome),
                }
            )
    return out


#: The one log line operators count when the gate could not run (there is no metrics
#: client in this package; this marker is what dashboards and alerts key on).
FAIL_OPEN_METRIC = "metric=sites_publish_gate_fail_open"


async def evaluate(
    pocket: dict[str, Any], *, pocket_id: str, artifacts: Any = None, store: Any = None
) -> tuple[list[dict[str, Any]], str]:
    """``(findings, skipped)``: the draft's owner-worded blocking findings, and why the
    page checks had nothing to read (``""`` when they ran). A checker crash is
    ``check_crashed`` with no findings, logged with :data:`FAIL_OPEN_METRIC`; pages left
    unread by the budget are ``pages_skipped``. Only ``""`` vouches for the content. The checks
    run in a worker thread under their own cooperative time budget
    (``verify_checks.MAX_CHECK_SECONDS``), so nothing is left running afterwards."""
    from pocketpaw_ee.sites import verify, verify_checks

    started = time.monotonic()
    try:
        checks = await asyncio.to_thread(
            verify.publish_checks, pocket, pocket_id, artifacts=artifacts, store=store
        )
    except Exception:  # noqa: BLE001 — the gate never makes publishing depend on itself
        logger.warning(
            "sites.publish_gate: %s reason=check_crashed pocket=%s",
            FAIL_OPEN_METRIC,
            pocket_id,
            exc_info=True,
        )
        return [], "check_crashed"
    findings = owner_findings(verify_checks.blocking(checks))
    skipped = str(checks.get("skipped") or "")
    if not skipped and checks.get("pages_skipped"):
        # Some pages were not read: the findings are real, but nothing vouches for the rest.
        skipped = "pages_skipped"
    logger.info(
        "sites.publish_gate: pocket=%s blocking=%d skipped=%s elapsed_ms=%d",
        pocket_id,
        len(findings),
        skipped,
        (time.monotonic() - started) * 1000,
    )
    return findings, skipped


async def blocking_findings(
    pocket: dict[str, Any], *, pocket_id: str, artifacts: Any = None, store: Any = None
) -> list[dict[str, Any]]:
    """The findings half of :func:`evaluate`."""
    findings, _skipped = await evaluate(
        pocket, pocket_id=pocket_id, artifacts=artifacts, store=store
    )
    return findings


async def pocket_check(
    *, pocket_id: str, user_id: str, artifacts: Any = None, store: Any = None
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
    """``(pocket, findings, skipped)`` for a pocket read by id as ``user_id`` (the review
    card and the approve path hold ids). A pocket that cannot be read is
    ``(None, [], "pocket_unreadable")``; the publish that follows reads it again."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    try:
        pocket = await pockets_service.get(pocket_id, user_id)
    except Exception:  # noqa: BLE001 — same fail-open rule as the checks
        logger.warning("sites.publish_gate: could not read pocket %s", pocket_id, exc_info=True)
        return None, [], "pocket_unreadable"
    findings, skipped = await evaluate(
        pocket, pocket_id=pocket_id, artifacts=artifacts, store=store
    )
    return pocket, findings, skipped


async def pocket_findings(
    *, pocket_id: str, user_id: str, artifacts: Any = None, store: Any = None
) -> list[dict[str, Any]]:
    """The findings half of :func:`pocket_check`."""
    _pocket, findings, _skipped = await pocket_check(
        pocket_id=pocket_id, user_id=user_id, artifacts=artifacts, store=store
    )
    return findings


def refusal(findings: list[dict[str, Any]]) -> PublishBlocked:
    """The :class:`PublishBlocked` for ``findings``, capped at :data:`MAX_FINDINGS`."""
    return PublishBlocked(findings[:MAX_FINDINGS], more=max(0, len(findings) - MAX_FINDINGS))


def last_gated(pocket_id: str, *, store: Any = None) -> str:
    """The content hash that last passed the gate (or that the owner confirmed)."""
    from pocketpaw_ee.sites import verify_store

    store = store if store is not None else verify_store.default_verify_store()
    try:
        record = store.read(pocket_id, verify_store.PUBLISH_GATE_KEY) or {}
    except Exception:  # noqa: BLE001 — no record: the next machine republish is gated
        return ""
    return str(record.get("content_hash") or "")


def remember_gated(pocket_id: str, content_hash: str, *, store: Any = None) -> None:
    """Remember ``content_hash`` as vetted, so a machine republish of exactly this
    content needs no second check. Call it ONLY when the check ran in full
    (``skipped == ""``) or the owner confirmed. Best effort: a lost record costs one
    re-check."""
    from pocketpaw_ee.sites import verify_store

    if not content_hash:
        return
    store = store if store is not None else verify_store.default_verify_store()
    try:
        store.write(
            pocket_id,
            verify_store.PUBLISH_GATE_KEY,
            {"content_hash": content_hash, "at": time.time()},
        )
    except Exception:  # noqa: BLE001
        logger.warning("sites.publish_gate: could not remember the gated hash for %s", pocket_id)


def record_blocked(pocket_id: str, details: Any, *, store: Any = None) -> None:
    """Keep the async lane's refusal (``PublishBlocked.details``) where the build status
    reads it. Best effort."""
    from pocketpaw_ee.sites import verify_store

    if not pocket_id:
        return
    store = store if store is not None else verify_store.default_verify_store()
    try:
        store.write(
            pocket_id, verify_store.PUBLISH_BLOCKED_KEY, {"details": details, "at": time.time()}
        )
    except Exception:  # noqa: BLE001
        logger.warning("sites.publish_gate: could not record the refusal for %s", pocket_id)


def read_blocked(pocket_id: str, *, store: Any = None) -> dict[str, Any] | None:
    """The last async-lane refusal's ``details`` for this pocket, or None."""
    from pocketpaw_ee.sites import verify_store

    store = store if store is not None else verify_store.default_verify_store()
    try:
        record = store.read(pocket_id, verify_store.PUBLISH_BLOCKED_KEY) or {}
    except Exception:  # noqa: BLE001
        return None
    details = record.get("details")
    return details if isinstance(details, dict) else None


async def audit_override(
    *, workspace_id: str, actor_id: str, pocket_id: str, findings: list[dict[str, Any]]
) -> None:
    """Write who took a site live past its findings, when (the audit row's time) and
    which findings, to the workspace audit log."""
    from pocketpaw_ee.cloud.audit import service as audit_service

    await audit_service.record(
        workspace_id,
        actor_id,
        "sites.publish_gate_overridden",
        target_type="pocket",
        target_id=pocket_id,
        metadata={
            "count": len(findings),
            "findings": [
                {k: f.get(k) for k in ("rule", "reason", "page", "text")}
                for f in findings[:MAX_FINDINGS]
            ],
        },
    )


async def gate_publish(
    pocket: dict[str, Any],
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    confirmed: bool = False,
    machine_republish: bool = False,
    artifacts: Any = None,
    store: Any = None,
) -> bool:
    """The gate in front of a live ``publish_pocket``. Raises :class:`PublishBlocked`
    on blocking findings unless the owner ``confirmed`` (then the override is audited).

    A ``machine_republish`` (plan sale, payment restamp, AI-visibility fix, an approved
    plan request) is exempt only when the content is exactly what last passed the gate
    or was confirmed; otherwise it is gated like any publish. Passing content is
    remembered. Returns True when the draft had no stored build to read (svelte /
    react), so the publish must check the build it makes (``check_built_output``)."""
    from pocketpaw_ee.sites import verify
    from pocketpaw_ee.sites.engines import normalize_engine

    try:
        content_hash = verify.publish_content_hash(pocket)
    except Exception:  # noqa: BLE001 — no hash: never exempt, still gated
        content_hash = ""
    if machine_republish and content_hash and content_hash == last_gated(pocket_id, store=store):
        return False
    findings, skipped = await evaluate(
        pocket, pocket_id=pocket_id, artifacts=artifacts, store=store
    )
    owner_confirmed = confirmed and not machine_republish
    if findings and not owner_confirmed:
        raise refusal(findings)
    if findings:
        await audit_override(
            workspace_id=workspace_id, actor_id=user_id, pocket_id=pocket_id, findings=findings
        )
    if not skipped or owner_confirmed:
        remember_gated(pocket_id, content_hash, store=store)
    engine = normalize_engine(pocket.get("engine"))
    return skipped == "no_built_output" and engine in ("svelte", "react") and not owner_confirmed


#: Built output read by :func:`check_built_output`; past it, the rest is not read.
_BUILT_MAX_BYTES = 12 * 1024 * 1024
_BUILT_SUFFIXES = (".html", ".htm", ".js", ".mjs")


def check_built_output(project_dir: str, engine: str, *, pocket_id: str) -> None:
    """Run the page checks on the tree a publish just built (its served output dir),
    right before the deploy, and raise :class:`PublishBlocked` on blocking findings.
    Blocking (file reads): call it from a worker thread. A crash is logged with
    :data:`FAIL_OPEN_METRIC` and lets the deploy go on."""
    from pathlib import Path

    from pocketpaw_ee.sites import verify_checks
    from pocketpaw_ee.sites.engines import resolve_static_output_rel

    try:
        root = Path(project_dir) / resolve_static_output_rel(project_dir, engine)
        files: dict[str, bytes] = {}
        total = 0
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            if any(part == "node_modules" or part.startswith(".") for part in rel.parts):
                continue
            if not path.is_file() or path.suffix.lower() not in _BUILT_SUFFIXES:
                continue
            data = path.read_bytes()
            total += len(data)
            if total > _BUILT_MAX_BYTES:
                break
            files[rel.as_posix()] = data
        checks = verify_checks.run_checks(files, engine=engine)
    except Exception:  # noqa: BLE001
        logger.warning(
            "sites.publish_gate: %s reason=built_output_check_crashed pocket=%s",
            FAIL_OPEN_METRIC,
            pocket_id,
            exc_info=True,
        )
        return
    findings = owner_findings(verify_checks.blocking(checks))
    if findings:
        raise refusal(findings)


__all__ = [
    "FAIL_OPEN_METRIC",
    "FORM_RULE",
    "OVERRIDE_FIELD",
    "PublishBlocked",
    "audit_override",
    "blocking_findings",
    "check_built_output",
    "evaluate",
    "gate_publish",
    "last_gated",
    "owner_findings",
    "page_label",
    "pocket_check",
    "pocket_findings",
    "read_blocked",
    "record_blocked",
    "refusal",
    "remember_gated",
]

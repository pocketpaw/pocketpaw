# ee/pocketpaw_ee/sites/publish_gate.py — "make sure it works before it goes live".
#
# A live publish (``service.publish_pocket``, REST and the agent's publish tool) is
# refused with :class:`PublishBlocked` (409 ``sites.publish_blocked``) while the draft
# still has a BLOCKING page finding: a form that saves nothing (SC-LINK-04), placeholder
# copy such as "[Address]" (SC-HONEST-07), a made-up phone number (SC-HONEST-03), or a
# form that failed when ``play_test_site`` submitted test data to it (SC-FORM-01). The
# owner can still take it live on purpose: ``confirm_placeholders=True`` skips the gate.
#
# Fast and never a rebuild: the findings come from ``verify.publish_checks``, which
# reads the draft's stored build output (or an html/no-build source) plus the stored
# form-exercise record for the same content hash. A check that cannot run (no build
# yet, a crash, over :data:`GATE_TIMEOUT_S`) lets the publish through and logs it: the
# gate exists to stop known defects, not to make publishing depend on the checker.
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

#: The longest the gate waits for the page checks before letting the publish through.
GATE_TIMEOUT_S = 15.0
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


async def blocking_findings(
    pocket: dict[str, Any], *, pocket_id: str, artifacts: Any = None, store: Any = None
) -> list[dict[str, Any]]:
    """The draft's owner-worded blocking findings, ``[]`` when there are none or the
    checks could not run (logged)."""
    from pocketpaw_ee.sites import verify, verify_checks

    started = time.monotonic()
    try:
        checks = await asyncio.wait_for(
            asyncio.to_thread(
                verify.publish_checks, pocket, pocket_id, artifacts=artifacts, store=store
            ),
            GATE_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 — the gate never makes publishing depend on itself
        logger.warning("sites.publish_gate: checks did not run for %s", pocket_id, exc_info=True)
        return []
    findings = owner_findings(verify_checks.blocking(checks))
    logger.info(
        "sites.publish_gate: pocket=%s blocking=%d skipped=%s elapsed_ms=%d",
        pocket_id,
        len(findings),
        checks.get("skipped", ""),
        (time.monotonic() - started) * 1000,
    )
    return findings


async def enforce(
    pocket: dict[str, Any],
    *,
    pocket_id: str,
    confirmed: bool = False,
    artifacts: Any = None,
    store: Any = None,
) -> None:
    """Raise :class:`PublishBlocked` when the draft has blocking findings and the owner
    has not confirmed publishing anyway."""
    if confirmed:
        return
    findings = await blocking_findings(
        pocket, pocket_id=pocket_id, artifacts=artifacts, store=store
    )
    if findings:
        raise PublishBlocked(findings[:MAX_FINDINGS], more=max(0, len(findings) - MAX_FINDINGS))


__all__ = [
    "FORM_RULE",
    "GATE_TIMEOUT_S",
    "OVERRIDE_FIELD",
    "PublishBlocked",
    "blocking_findings",
    "enforce",
    "owner_findings",
    "page_label",
]

# ee/pocketpaw_ee/cloud/jobs/builtin/score_applications.py
# Created: 2026-06-20 (feat/workspace-jobs, pp#1459) — the first built-in
# workspace job. V1 was a thin, side-effect-free reference: a toy `len(name)`
# score over `params["rows"]`, projected through a PII allowlist before the
# result became broadcast `state`. The allowlist is the security point of the
# built-in — a job result is merged into the pocket spec and fanned out over the
# realtime bus to every open canvas, so a job MUST strip email / phone (and any
# non-allowlisted field) so PII never lands in the broadcast/audit path.
#
# Updated: 2026-06-22 (fix/jobs-real-builtin-and-status) — turned the toy job
# into a REAL data-backed job so the primitive can demo on live records, not
# fixture rows hardcoded in a pocket spec:
#   - SOURCE READ: when `params["source_collection"]` is set, the job reads
#     records from that Mongo collection in the cloud DB through
#     `jobs.service.read_source_records` (the jobs entity owns DB access — no
#     second MongoClient, no direct Beanie-doc import). Bounded by `batch_size`.
#   - IDEMPOTENT BATCH ADVANCEMENT: the job fetches the pocket's CURRENT
#     `scored_rows` (via `pockets.service.get_pocket_ripple_spec`), SKIPS source
#     records whose id is already scored, scores only the NEW ones, and returns
#     the ACCUMULATED rows (existing + new) so the canvas grows by a batch per
#     run — re-running pulls the next batch.
#   - REAL HEURISTIC: a deterministic 0-100 field-based score (completeness +
#     referral/social signal − disposable-email − sparsity), a `band`
#     (Strong/Moderate/Weak), and `stage:"scored"`. Replaces `len(name)`.
#   - PII ALLOWLIST still applies and is extended with `band`; email/phone/raw
#     fields are stripped before a row reaches `state`.
#   - FALLBACK: with no `source_collection`, the job keeps the existing
#     `params["rows"]` behavior so current tests/specs still work.
# The WORKER now owns the `<action>_status` flag (Bug B fix), so this built-in
# no longer returns any status key — only `scored_rows` + the scored count.
#
# Updated: 2026-06-22 (feat/jobs-worker-register-and-connector-read) — added a
# CONNECTOR source mode: when `params` carries `connector` (and `action`), the
# job reads its batch from the workspace's BOUND connector via
# `jobs.service.execute_connector_action` (the jobs entity owns the connectors
# call — the built-in never imports the connectors service directly) and scores
# those records. Source PRECEDENCE is connector > source_collection > rows; the
# Mongo `source_collection` and inline `rows` fallbacks are PRESERVED. The same
# idempotent dedup (skip ids already in the pocket's `scored_rows`), the same
# real heuristic + band, and the same PII allowlist (id/name/score/band/stage)
# apply to all three source modes — only WHERE the batch comes from differs.
#
# Updated: 2026-07-14 (fix/jobs-source-collection-tenancy) — the Mongo
# `source_collection` read now threads this job's `workspace_id` into
# `jobs.service.read_source_records(..., workspace_id=workspace_id, ...)`, which
# scopes the query to the job's own workspace and denylists credential/system
# collections. Closes a P1 cross-tenant leak: an author-controlled
# `source_collection` previously read EVERY tenant's rows from the shared DB.
# Connector + inline-`rows` source modes are unchanged (already tenant-safe).

"""Built-in `score_applications` job: real data-backed scoring + PII allowlist."""

from __future__ import annotations

import re
from typing import Any

from pocketpaw_ee.cloud.jobs import service as jobs_service
from pocketpaw_ee.cloud.pockets import service as pockets_service

# The ONLY fields a scored row may broadcast. Everything else — notably any
# email / phone / free-text contact field — is dropped before the row reaches
# `state`. Allowlist (not denylist) so a new raw field is dropped by default.
_ALLOWED_ROW_FIELDS = ("id", "name", "score", "band", "stage")

# Generic field fallbacks — kept deliberately broad so the job works on any
# application/lead/submission shape, not just one customer's schema.
_ID_FIELDS = ("id", "_id", "application_id", "submission_id")
_NAME_FIELDS = ("name", "fullName", "full_name", "firstName", "first_name")
_EMAIL_FIELDS = ("email", "emailAddress", "email_address")
_MESSAGE_FIELDS = ("message", "what_brings_you", "bio", "answers", "notes", "about")
_REFERRAL_FIELDS = ("referral", "referred_by", "referrer", "linkedin", "twitter", "social")

# Disposable / throwaway email domains — a negative signal. Substring match so
# subdomains (e.g. `foo.mailinator.com`) are caught too.
_DISPOSABLE_PATTERNS = (
    "mailinator",
    "tempmail",
    "temp-mail",
    "throwaway",
    "guerrillamail",
    "10minutemail",
    "trashmail",
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def project_row(row: dict[str, Any]) -> dict[str, Any]:
    """Project one scored row to the broadcast-safe allowlist.

    Drops every field not in :data:`_ALLOWED_ROW_FIELDS`, so PII like
    ``email`` / ``phone`` can never reach the broadcast `state`.
    """
    return {k: row[k] for k in _ALLOWED_ROW_FIELDS if k in row}


def _first_present(row: dict[str, Any], fields: tuple[str, ...]) -> Any:
    """Return the first non-empty value among ``fields``, else ``None``."""
    for f in fields:
        val = row.get(f)
        if val is not None and str(val).strip() != "":
            return val
    return None


def _record_id(row: dict[str, Any]) -> str | None:
    """Resolve a stable id for dedup. ``None`` when the record has none."""
    val = _first_present(row, _ID_FIELDS)
    return str(val) if val is not None else None


def _band_for(score: int) -> str:
    """Map a 0-100 score to a coarse band."""
    if score >= 70:
        return "Strong"
    if score >= 40:
        return "Moderate"
    return "Weak"


def _heuristic_score(row: dict[str, Any]) -> int:
    """Deterministic field-based score, clamped to 0-100.

    Rewards completeness (name + a valid-looking email + a non-trivial
    message/answers/bio field) and a referral/social signal; penalizes a
    disposable-email pattern and very sparse records. Pure + deterministic so
    re-running on the same record yields the same score.
    """
    score = 0

    name = _first_present(row, _NAME_FIELDS)
    if name is not None:
        score += 20

    email = _first_present(row, _EMAIL_FIELDS)
    email_str = str(email).strip().lower() if email is not None else ""
    if email_str:
        if _EMAIL_RE.match(email_str):
            score += 20
        else:
            score += 5  # present but malformed — minor credit
        if any(p in email_str for p in _DISPOSABLE_PATTERNS):
            score -= 30  # disposable address — strong negative

    message = _first_present(row, _MESSAGE_FIELDS)
    if message is not None:
        text = message if isinstance(message, str) else str(message)
        length = len(text.strip())
        if length >= 120:
            score += 30
        elif length >= 40:
            score += 20
        elif length > 0:
            score += 8

    if _first_present(row, _REFERRAL_FIELDS) is not None:
        score += 15  # referral / social signal

    # Sparsity penalty — a record with almost nothing in it is low-signal.
    populated = sum(1 for v in row.values() if v is not None and str(v).strip() != "")
    if populated <= 1:
        score -= 20
    elif populated == 2:
        score -= 8

    return max(0, min(100, score))


def _score_row(row: dict[str, Any]) -> dict[str, Any]:
    """Score one source record and project it to the broadcast-safe allowlist.

    Carries the resolved id + name through so the projected row stays useful
    even when the source used a fallback field name (``_id`` / ``fullName``).
    """
    rid = _record_id(row)
    name = _first_present(row, _NAME_FIELDS)
    score = _heuristic_score(row)
    scored = {
        **row,
        "id": rid,
        "name": name if name is not None else "",
        "score": score,
        "band": _band_for(score),
        "stage": "scored",
    }
    return project_row(scored)


def _already_scored_ids(existing_rows: list[Any]) -> set[str]:
    """Collect the ids already present in the pocket's ``scored_rows``."""
    ids: set[str] = set()
    for r in existing_rows:
        if isinstance(r, dict):
            rid = r.get("id")
            if rid is not None:
                ids.add(str(rid))
    return ids


class ScoreApplicationsJob:
    """Score a batch of applications and write the scored rows to `state`.

    Runs under the workspace service identity. Returns a STATE-ONLY partial
    spec — `scored_rows` (PII-stripped) and the scored count. The WORKER owns
    the `<action>_status` flag, so this built-in returns no status key.
    """

    name = "score_applications"

    async def __call__(
        self, *, workspace_id: str, pocket_id: str, job_id: str, params: dict
    ) -> dict:
        batch_size = max(0, int(params.get("batch_size", 20)))
        connector = params.get("connector")
        connector_action = params.get("action")
        source_collection = params.get("source_collection")

        # Source PRECEDENCE: connector > source_collection > inline rows. A
        # `connector` (with its `action`) reads the batch from the workspace's
        # bound connector; otherwise a `source_collection` reads from Mongo;
        # otherwise the inline `params["rows"]` fallback keeps current
        # specs/tests working.
        if (
            isinstance(connector, str)
            and connector.strip()
            and isinstance(connector_action, str)
            and connector_action.strip()
        ):
            scored_rows = await self._run_connector_backed(
                workspace_id=workspace_id,
                pocket_id=pocket_id,
                connector=connector,
                connector_action=connector_action,
                connector_params=params.get("connector_params"),
                batch_size=batch_size,
            )
        elif isinstance(source_collection, str) and source_collection.strip():
            scored_rows = await self._run_source_backed(
                workspace_id=workspace_id,
                pocket_id=pocket_id,
                source_collection=source_collection,
                batch_size=batch_size,
            )
        else:
            # Fallback — score the rows passed in `params["rows"]` directly.
            rows = list(params.get("rows", []))[:batch_size]
            scored_rows = [_score_row(r) for r in rows if isinstance(r, dict)]

        return {
            "state": {
                "scored_rows": scored_rows,
                "score_applications_scored_count": len(scored_rows),
            }
        }

    async def _existing_scored_rows(
        self, *, workspace_id: str, pocket_id: str
    ) -> list[dict[str, Any]]:
        """The pocket's CURRENT ``scored_rows`` — the dedup + accumulation base.

        Shared by every data-backed source mode (connector + collection) so the
        idempotent batch advancement is identical regardless of where the batch
        comes from.
        """
        spec = await pockets_service.get_pocket_ripple_spec(workspace_id, pocket_id)
        state = spec.get("state") if isinstance(spec, dict) else None
        existing_rows = state.get("scored_rows") if isinstance(state, dict) else None
        return list(existing_rows) if isinstance(existing_rows, list) else []

    def _score_next_batch(
        self,
        *,
        existing_rows: list[dict[str, Any]],
        page: list[Any],
        batch_size: int,
    ) -> list[dict[str, Any]]:
        """Score the next NEW batch from ``page`` and return the ACCUMULATED rows.

        Idempotent: records whose id is already in ``existing_rows`` are skipped,
        so re-running advances to the next batch instead of re-scoring the same
        records. Records with no resolvable id are skipped (can't dedup). The new
        batch is capped at ``batch_size``; the result is ``existing_rows`` plus
        the newly-scored ones (PII-projected by :func:`_score_row`).
        """
        seen_ids = _already_scored_ids(existing_rows)
        if batch_size == 0:
            return existing_rows

        new_scored: list[dict[str, Any]] = []
        for record in page:
            if not isinstance(record, dict):
                continue
            rid = _record_id(record)
            if rid is None or rid in seen_ids:
                continue
            new_scored.append(_score_row(record))
            seen_ids.add(rid)
            if len(new_scored) >= batch_size:
                break

        return existing_rows + new_scored

    async def _run_connector_backed(
        self,
        *,
        workspace_id: str,
        pocket_id: str,
        connector: str,
        connector_action: str,
        connector_params: Any,
        batch_size: int,
    ) -> list[dict[str, Any]]:
        """Read a batch from a BOUND connector, score the next NEW records, and
        return the ACCUMULATED rows (existing scored + newly scored).

        The connector read goes through ``jobs.service.execute_connector_action``
        (the jobs entity owns the connectors call); on a connector failure that
        helper raises ``CloudError`` which propagates to the worker's failure
        path. Same idempotent dedup as the Mongo path: records already scored on
        the pocket are skipped. The connector's data payload may be a list of
        records (the common shape) or a single record dict (normalized to a
        one-element list).
        """
        existing_rows = await self._existing_scored_rows(
            workspace_id=workspace_id, pocket_id=pocket_id
        )
        if batch_size == 0:
            return existing_rows

        params = connector_params if isinstance(connector_params, dict) else {}
        data = await jobs_service.execute_connector_action(
            workspace_id, connector, connector_action, params
        )
        # Normalize the connector data payload to a list of records: a
        # list-shaped action returns the records directly; a scalar one returns a
        # single dict we wrap. Anything else reads as "no records".
        if isinstance(data, list):
            page: list[Any] = data
        elif isinstance(data, dict):
            page = [data]
        else:
            page = []

        return self._score_next_batch(existing_rows=existing_rows, page=page, batch_size=batch_size)

    async def _run_source_backed(
        self,
        *,
        workspace_id: str,
        pocket_id: str,
        source_collection: str,
        batch_size: int,
    ) -> list[dict[str, Any]]:
        """Read a page of live source records, score the next NEW batch, and
        return the ACCUMULATED rows (existing scored + newly scored).

        Idempotent: records whose id is already in the pocket's current
        ``scored_rows`` are skipped, so re-running advances to the next batch
        instead of re-scoring the same records.
        """
        existing_rows = await self._existing_scored_rows(
            workspace_id=workspace_id, pocket_id=pocket_id
        )
        if batch_size == 0:
            return existing_rows

        # Read a bounded page from the source. Pull a few-hundred window so the
        # next unscored batch is in reach without an unbounded scan; the page is
        # then filtered to NEW records and capped at `batch_size`. The read is
        # SCOPED to this job's `workspace_id` (and denylists credential/system
        # collections) inside `read_source_records`, so an author-controlled
        # `source_collection` can only ever surface this workspace's own rows.
        page = await jobs_service.read_source_records(
            source_collection, workspace_id=workspace_id, limit=300
        )

        return self._score_next_batch(existing_rows=existing_rows, page=page, batch_size=batch_size)


__all__ = ["ScoreApplicationsJob", "project_row"]

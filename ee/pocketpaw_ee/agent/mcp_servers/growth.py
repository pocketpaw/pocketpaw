# ee/pocketpaw_ee/agent/mcp_servers/growth.py — the agent-facing /growth surface.
#
# In-process MCP server (``pocketpaw_growth``) letting the chat agent on the
# /growth rail work a prospect end to end: read the list, read one prospect with
# its drafts, add a prospect it just researched, write and revise the copy, and
# FILE the send for human approval. It stops there.
#
# THE LINE THAT MATTERS — the agent's reach ends at ``proposed``:
#   * NO tool sends. Not one. The dispatch worker sends, and only after a human
#     approves an Instinct proposal in the Tray.
#   * NO tool can reach a status in ``GATE_OWNED_TARGETS`` (``approved`` /
#     ``sent``). No tool takes a ``status`` argument at all — the legal moves are
#     exposed as named verbs (``growth_propose_send``) instead, so there is no
#     shape of argument that could ask for ``approved``.
#   * ``service.gate_transition`` (the seam the executor and the dispatch worker
#     walk) is deliberately NOT imported here, and a test asserts it stays that
#     way. Same reason ``connectors/mailtrap.yaml`` ships ``actions: []``: every
#     bypass around the gate has to stay closed, including a friendly-looking
#     one.
#   * ``growth_update_draft`` edits only a draft still in ``draft``; the service
#     refuses the rest (403 ``draft.not_editable``). Editing a proposed draft
#     would put copy on the wire that nobody approved — that is a send bypass
#     wearing an edit's clothes.
#
# Every tool calls the SERVICE layer (``ee.cloud.growth.service``), never a
# Beanie document: the service owns tenancy filtering, validation and the
# proposal filing, and the import-linter "Growth" contract lists this module so
# a doc import here fails CI.
#
# Tenancy: the workspace is resolved from the chat stream's identity ContextVars
# (``ee.cloud.chat.agent_service``), exactly as belt.py / ship.py do. No tool
# accepts a ``workspace_id`` argument — the model cannot name a tenant.
#
# RBAC: the same per-action tiers the HTTP routes carry — ``growth.read`` on the
# reads, ``growth.write`` on the authoring writes, ``growth.manage`` (ADMIN) on
# the propose verbs. The propose tier is not decoration: ``growth.executor``
# re-checks ``growth.manage`` against the proposer's CURRENT role at approve
# time, so a proposal filed under a lower tier could never be approved — it
# would just clog the Tray. A denial comes back as a structured envelope
# (``ok:false, denied:true``), never a raised exception (mirrors
# workspace_admin.py).
#
# Output shaping: the list tools return COMPACT rows (id / name / company /
# domain / tier / status) plus the filter-scoped ``total``, never every field of
# every row — a 100-row context dump buys nothing and costs the turn. Full
# bodies appear only where the agent needs them to act: one prospect's drafts,
# and the LinkedIn queue.
#
# Created 2026-07-28 (feat/growth-mcp): new module.
# Updated 2026-07-28 (feat/growth-projects): a prospect may now be JUST A DOMAIN
# — ``CreateProspectRequest`` accepts an empty name / company. The CREATE
# refusal in ``_upsert_prospect_handler`` is UNCHANGED and is now the only thing
# holding that line for the agent: a human pasting a domain list is entitled to
# a nameless row, an agent that researched a company and still can't name it is
# not. What did change is the placeholder: the ``or "unknown"`` fallbacks are
# gone, because with an empty name legal in the store they would have stamped
# the literal word "unknown" over a real (blank) value on any enrichment call.

from __future__ import annotations

import json
import logging
from typing import Any

from ._audit import record_tool_call

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_growth"

# Stable tool ids — Claude Code namespaces in-process MCP tools as
# ``mcp__<server>__<tool>``; a surface profile allowlist would reference these.
GROWTH_TOOL_NAMES: tuple[str, ...] = (
    "growth_list_prospects",
    "growth_get_prospect",
    "growth_list_drafts",
    "growth_upsert_prospect",
    "growth_create_draft",
    "growth_update_draft",
    "growth_propose_send",
    "growth_propose_send_batch",
    "growth_linkedin_queue",
)

GROWTH_TOOL_IDS: tuple[str, ...] = tuple(f"mcp__{SERVER_NAME}__{n}" for n in GROWTH_TOOL_NAMES)

# RBAC actions, mirroring the HTTP router's per-route guards.
READ_ACTION = "growth.read"
WRITE_ACTION = "growth.write"
MANAGE_ACTION = "growth.manage"

# Page sizes. The agent reads to DECIDE, not to recite: a big page is context
# spent on rows it will not act on. ``_MAX_LIST_LIMIT`` caps what the model can
# ask for; ``total`` tells it how much it did not see.
_DEFAULT_LIST_LIMIT = 25
_MAX_LIST_LIMIT = 100

# How wide the upsert's existing-row lookup searches before giving up on
# preserving the row's lifecycle status. See ``_find_by_domain``.
_UPSERT_LOOKUP_LIMIT = 100


# ---------------------------------------------------------------------------
# Envelopes + identity
# ---------------------------------------------------------------------------


def _error_response(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {message}"}], "is_error": True}


def _success_response(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(body, separators=(",", ":"), default=str)}]
    }


def _identity() -> tuple[str | None, str | None]:
    """Resolve (workspace_id, user_id) from the cloud chat session context."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id

        return current_workspace_id(), current_user_id()
    except Exception:  # noqa: BLE001 — no cloud session context
        return None, None


def _build_ctx(workspace_id: str, user_id: str) -> Any:
    """Synthesise the ``RequestContext`` the growth service takes. The chat
    stream has no FastAPI request scope, so we build one from the resolved
    identity — the same thing tasks.py / workspace_admin.py do."""
    from datetime import UTC, datetime

    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind

    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="mcp-growth",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


async def _load_user(user_id: str) -> Any | None:
    """Load the User doc so the RBAC check has the ``.workspaces`` membership
    list it reads. ``None`` on a bad id / missing user — the caller maps that to
    an error envelope (fails closed)."""
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.user import User as _UserDoc

    try:
        return await _UserDoc.get(PydanticObjectId(user_id))
    except Exception:  # noqa: BLE001 — malformed id / DB error → treat as no user
        return None


async def _gate(tool: str, action: str) -> tuple[str, str, Any] | dict[str, Any]:
    """Resolve identity + apply the workspace RBAC gate for ``tool``.

    Returns ``(workspace_id, user_id, ctx)`` when the gate PASSES, or a
    ready-to-return response dict when it fails. A denial comes back as a
    structured envelope rather than a raised exception (the gate already audits
    it), so the agent can explain the refusal instead of the turn erroring out.
    """
    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            f"{tool} requires workspace and user context (call it from a cloud chat session)."
        )

    from pocketpaw_ee.guards.deps import check_workspace_action
    from pocketpaw_ee.guards.rbac import Forbidden

    user = await _load_user(user_id)
    if user is None:
        return _error_response("could not resolve the calling user for the permission check.")

    try:
        check_workspace_action(user, workspace_id, action)
    except Forbidden as exc:
        logger.info(
            "%s denied: user=%s workspace=%s code=%s", tool, user_id, workspace_id, exc.code
        )
        return _success_response(
            {
                "ok": False,
                "denied": True,
                "code": exc.code,
                "message": f"You do not have permission to run {tool} ({action}).",
            }
        )

    record_tool_call(
        workspace_id=workspace_id,
        user_id=user_id,
        tool_server=SERVER_NAME,
        tool_name=tool,
        status="ok",
        ok=True,
    )
    return workspace_id, user_id, _build_ctx(workspace_id, user_id)


def _service_error(exc: Exception, what: str) -> dict[str, Any]:
    """Relay a service failure plainly. A CloudError stringifies as
    ``code: message`` — both halves are safe to show and the code is what the
    agent should quote back."""
    logger.info("growth mcp: %s failed: %s", what, exc, exc_info=True)
    return _error_response(f"could not {what}: {exc}")


# ---------------------------------------------------------------------------
# Wire shaping — compact by default
# ---------------------------------------------------------------------------


def _prospect_row(p: Any) -> dict[str, Any]:
    """One list row: enough to pick a prospect, not enough to bloat the turn.
    The research brief and contact details live on ``growth_get_prospect``."""
    return {
        "id": p.id,
        "name": p.name,
        "company": p.company,
        "domain": p.domain,
        "tier": p.tier,
        "status": p.status,
        "source": p.source,
    }


def _prospect_full(p: Any) -> dict[str, Any]:
    return {
        **_prospect_row(p),
        "research_brief": p.research_brief,
        "emails": list(p.emails),
        "linkedin_url": p.linkedin_url,
        "whatsapp_number": p.whatsapp_number,
        "opted_in": p.opted_in,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


def _draft_row(d: Any, *, body: bool = True) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": d.id,
        "prospect_id": d.prospect_id,
        "channel": d.channel,
        "variant": d.variant,
        "status": d.status,
        "subject": d.subject,
    }
    if body:
        row["body"] = d.body
        row["demo_url"] = d.demo_url
    else:
        row["body_preview"] = _preview(d.body)
    return row


def _preview(text: str, max_len: int = 120) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= max_len else flat[: max_len - 1] + "…"


def _clamp_limit(raw: Any, default: int = _DEFAULT_LIST_LIMIT) -> int:
    try:
        return max(1, min(int(raw), _MAX_LIST_LIMIT))
    except (TypeError, ValueError):
        return default


def _opt_str(args: dict, key: str) -> str | None:
    """Read an optional string arg. Blank means "not supplied" — a model that
    fills every key with ``""`` must not be read as "clear this filter"."""
    value = args.get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


# ---------------------------------------------------------------------------
# Handlers — reads
# ---------------------------------------------------------------------------


async def _list_prospects_handler(args: dict) -> dict:
    gate = await _gate("growth_list_prospects", READ_ACTION)
    if isinstance(gate, dict):
        return gate
    _ws, _user, ctx = gate

    from pocketpaw_ee.cloud.growth import service

    limit = _clamp_limit(args.get("limit"))
    try:
        page = await service.list_prospects(
            ctx,
            tier=_opt_str(args, "tier"),
            status=_opt_str(args, "status"),
            source=_opt_str(args, "source"),
            q=_opt_str(args, "q"),
            sort=_opt_str(args, "sort") or "newest",
            cursor=_opt_str(args, "cursor"),
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001
        return _service_error(exc, "list prospects")

    return _success_response(
        {
            "prospects": [_prospect_row(p) for p in page.items],
            "showing": len(page.items),
            "total": page.total,
            "next_cursor": page.next_cursor,
            "note": (
                "Compact rows. Call growth_get_prospect for the research brief, "
                "contact details and drafts. `total` is the filter-scoped count, "
                "not the page; pass `next_cursor` back to read the next page."
            ),
        }
    )


async def _get_prospect_handler(args: dict) -> dict:
    gate = await _gate("growth_get_prospect", READ_ACTION)
    if isinstance(gate, dict):
        return gate
    _ws, _user, ctx = gate

    prospect_id = _opt_str(args, "prospect_id")
    if not prospect_id:
        return _error_response("growth_get_prospect requires a `prospect_id`.")

    from pocketpaw_ee.cloud.growth import service

    try:
        prospect = await service.get(ctx, prospect_id)
        drafts = await service.list_drafts(ctx, prospect_id=prospect_id, limit=_MAX_LIST_LIMIT)
    except Exception as exc:  # noqa: BLE001
        return _service_error(exc, "read the prospect")

    return _success_response(
        {
            "prospect": _prospect_full(prospect),
            "drafts": [_draft_row(d) for d in drafts],
        }
    )


async def _list_drafts_handler(args: dict) -> dict:
    gate = await _gate("growth_list_drafts", READ_ACTION)
    if isinstance(gate, dict):
        return gate
    _ws, _user, ctx = gate

    from pocketpaw_ee.cloud.growth import service

    try:
        drafts = await service.list_drafts(
            ctx,
            prospect_id=_opt_str(args, "prospect_id"),
            channel=_opt_str(args, "channel"),
            status=_opt_str(args, "status"),
            limit=_clamp_limit(args.get("limit")),
        )
    except Exception as exc:  # noqa: BLE001
        return _service_error(exc, "list drafts")

    return _success_response(
        {
            "drafts": [_draft_row(d, body=False) for d in drafts],
            "showing": len(drafts),
            "note": (
                "Bodies are truncated previews. Read the full copy with "
                "growth_get_prospect. `status` here is a READ filter — a draft's "
                "status is moved by proposing it and by a human approving it, "
                "never by an argument."
            ),
        }
    )


async def _linkedin_queue_handler(args: dict) -> dict:
    gate = await _gate("growth_linkedin_queue", READ_ACTION)
    if isinstance(gate, dict):
        return gate
    _ws, _user, ctx = gate

    from pocketpaw_ee.cloud.growth import service

    try:
        items = await service.linkedin_queue(ctx, limit=_clamp_limit(args.get("limit")))
    except Exception as exc:  # noqa: BLE001
        return _service_error(exc, "read the LinkedIn queue")

    return _success_response(
        {
            "queue": [
                {
                    "draft": _draft_row(item.draft),
                    "prospect_name": item.prospect_name,
                    "prospect_company": item.prospect_company,
                    "linkedin_url": item.linkedin_url,
                    "tier": item.tier,
                    "research_brief": item.research_brief,
                }
                for item in items
            ],
            "count": len(items),
            "note": (
                "LinkedIn sending is MANUAL by design — there is no API "
                "integration and no tool that sends. A human copies these and "
                "sends them, then marks them sent."
            ),
        }
    )


# ---------------------------------------------------------------------------
# Handlers — authoring writes
# ---------------------------------------------------------------------------


async def _find_by_domain(ctx: Any, domain: str) -> Any | None:
    """Find an existing prospect by its exact (normalised) domain.

    Used to make ``growth_upsert_prospect`` a MERGE rather than a replace.
    ``upsert_by_domain`` overwrites every mutable field from the request, so a
    re-upsert of a prospect already in ``in_sequence`` would silently reset it
    to ``new`` and blank the fields the agent did not repeat. Reading the row
    first lets the handler carry those forward.

    The lookup rides the service's ``q`` search (which covers the domain field)
    and exact-matches the result, so it needs no new service seam. On a miss the
    caller proceeds with defaults — a create, which is the correct behaviour for
    a domain that genuinely is not there.
    """
    from pocketpaw_ee.cloud.growth import service

    page = await service.list_prospects(ctx, q=domain, limit=_UPSERT_LOOKUP_LIMIT)
    for item in page.items:
        if item.domain == domain:
            return item
    return None


async def _upsert_prospect_handler(args: dict) -> dict:
    gate = await _gate("growth_upsert_prospect", WRITE_ACTION)
    if isinstance(gate, dict):
        return gate
    workspace_id, _user, ctx = gate

    from pocketpaw_ee.cloud.growth import service
    from pocketpaw_ee.cloud.growth.dto import CreateProspectRequest

    name = _opt_str(args, "name")
    company = _opt_str(args, "company")
    domain = _opt_str(args, "domain")
    if not domain:
        return _error_response("growth_upsert_prospect requires a company `domain`.")

    emails = args.get("emails")
    emails = (
        [e for e in emails if isinstance(e, str) and e.strip()]
        if isinstance(emails, list)
        else None
    )
    opted_in = args.get("opted_in") if isinstance(args.get("opted_in"), bool) else None

    try:
        # Build once to canonicalise the domain (the DTO validator strips the
        # scheme / www / path), then look the row up on that canonical form.
        probe = CreateProspectRequest(
            domain=domain,
            source=_opt_str(args, "source") or "manual",
        )
    except Exception as exc:  # noqa: BLE001 — malformed domain
        return _service_error(exc, "read the domain")

    try:
        existing = await _find_by_domain(ctx, probe.domain)
    except Exception as exc:  # noqa: BLE001
        return _service_error(exc, "look up the prospect")

    if existing is None and (not name or not company):
        # Only an ENRICHMENT call may omit these — there is nothing stored to
        # carry forward, and a row named "unknown / unknown" is worse than a
        # refusal the agent can act on.
        return _error_response(
            f"'{probe.domain}' is not in this workspace yet, so "
            "growth_upsert_prospect needs `name` and `company` to add it."
        )

    def _pick(supplied: Any, field: str, fallback: Any) -> Any:
        """Supplied wins; otherwise carry the stored value forward."""
        if supplied is not None:
            return supplied
        return getattr(existing, field) if existing is not None else fallback

    try:
        body = CreateProspectRequest(
            # No "unknown" placeholder: on a CREATE the refusal above already
            # demanded both, and on an ENRICH the stored value carries forward
            # — including a stored "" from a bare-domain import, which must
            # survive an unrelated enrichment rather than be stamped over.
            name=_pick(name, "name", "") or "",
            company=_pick(company, "company", "") or "",
            domain=probe.domain,
            source=_pick(_opt_str(args, "source"), "source", "manual"),
            tier=_pick(_opt_str(args, "tier"), "tier", "unqualified"),
            research_brief=_pick(_opt_str(args, "research_brief"), "research_brief", ""),
            emails=_pick(emails, "emails", []),
            linkedin_url=_pick(_opt_str(args, "linkedin_url"), "linkedin_url", None),
            whatsapp_number=_pick(_opt_str(args, "whatsapp_number"), "whatsapp_number", None),
            opted_in=_pick(opted_in, "opted_in", False),
            # NOT settable by the agent. The outbound lifecycle is driven by
            # what happens to the prospect (a draft written, a reply received,
            # the sweep giving up), never by an argument in a research call.
            status=existing.status if existing is not None else "new",
        )
    except Exception as exc:  # noqa: BLE001 — DTO rejection
        return _service_error(exc, "validate the prospect")

    try:
        result = await service.upsert_by_domain(workspace_id, body)
    except Exception as exc:  # noqa: BLE001
        return _service_error(exc, "save the prospect")

    return _success_response(
        {
            "ok": True,
            "created": existing is None,
            "prospect": _prospect_full(result),
        }
    )


async def _create_draft_handler(args: dict) -> dict:
    gate = await _gate("growth_create_draft", WRITE_ACTION)
    if isinstance(gate, dict):
        return gate
    _ws, _user, ctx = gate

    prospect_id = _opt_str(args, "prospect_id")
    if not prospect_id:
        return _error_response("growth_create_draft requires a `prospect_id`.")

    from pocketpaw_ee.cloud.growth import service
    from pocketpaw_ee.cloud.growth.dto import CreateDraftRequest

    try:
        body = CreateDraftRequest(
            channel=_opt_str(args, "channel") or "email",
            subject=_opt_str(args, "subject"),
            body=args.get("body") if isinstance(args.get("body"), str) else "",
            variant=_opt_str(args, "variant") or "first_touch",
            demo_url=_opt_str(args, "demo_url"),
        )
    except Exception as exc:  # noqa: BLE001 — DTO rejection
        return _service_error(exc, "validate the draft")

    try:
        draft = await service.create_draft(ctx, prospect_id, body)
    except Exception as exc:  # noqa: BLE001
        return _service_error(exc, "create the draft")

    return _success_response(
        {
            "ok": True,
            "draft": _draft_row(draft),
            "note": (
                "The draft is saved as `draft`. Nothing is queued and nothing "
                "will send until you propose it and a human approves it in the Tray."
            ),
        }
    )


async def _update_draft_handler(args: dict) -> dict:
    gate = await _gate("growth_update_draft", WRITE_ACTION)
    if isinstance(gate, dict):
        return gate
    _ws, _user, ctx = gate

    draft_id = _opt_str(args, "draft_id")
    if not draft_id:
        return _error_response("growth_update_draft requires a `draft_id`.")

    from pocketpaw_ee.cloud.growth import service
    from pocketpaw_ee.cloud.growth.dto import UpdateDraftRequest

    try:
        body = UpdateDraftRequest(
            subject=_opt_str(args, "subject"),
            body=args.get("body") if isinstance(args.get("body"), str) else None,
            demo_url=_opt_str(args, "demo_url"),
        )
    except Exception as exc:  # noqa: BLE001 — DTO rejection (nothing to change, blank body)
        return _service_error(exc, "validate the edit")

    try:
        draft = await service.update_draft(ctx, draft_id, body)
    except Exception as exc:  # noqa: BLE001
        return _service_error(exc, "edit the draft")

    return _success_response({"ok": True, "draft": _draft_row(draft)})


# ---------------------------------------------------------------------------
# Handlers — propose (the end of the agent's reach)
# ---------------------------------------------------------------------------

_PROPOSE_NOTE = (
    "NOTHING has been sent. The draft is now `proposed` and waiting in the "
    "Instinct Tray for a human to approve or reject. Only after approval does "
    "the dispatch worker send it. Never tell the user their message went out."
)


async def _propose_send_handler(args: dict) -> dict:
    gate = await _gate("growth_propose_send", MANAGE_ACTION)
    if isinstance(gate, dict):
        return gate
    _ws, _user, ctx = gate

    draft_id = _opt_str(args, "draft_id")
    if not draft_id:
        return _error_response("growth_propose_send requires a `draft_id`.")

    from pocketpaw_ee.cloud.growth import service

    try:
        result = await service.propose_send(ctx, draft_id)
    except Exception as exc:  # noqa: BLE001
        return _service_error(exc, "propose the send")

    return _success_response(
        {
            "ok": True,
            "status": "proposed",
            "proposal_id": result.proposal_id,
            "draft": _draft_row(result.draft, body=False),
            "note": _PROPOSE_NOTE,
        }
    )


async def _propose_send_batch_handler(args: dict) -> dict:
    gate = await _gate("growth_propose_send_batch", MANAGE_ACTION)
    if isinstance(gate, dict):
        return gate
    _ws, _user, ctx = gate

    raw = args.get("draft_ids")
    draft_ids = (
        [d.strip() for d in raw if isinstance(d, str) and d.strip()]
        if isinstance(raw, list)
        else []
    )
    if not draft_ids:
        return _error_response("growth_propose_send_batch requires a non-empty `draft_ids` list.")

    from pocketpaw_ee.cloud.growth import service
    from pocketpaw_ee.cloud.growth.dto import ProposeBatchRequest

    try:
        result = await service.propose_send_batch(ctx, ProposeBatchRequest(draft_ids=draft_ids))
    except Exception as exc:  # noqa: BLE001
        return _service_error(exc, "propose the batch")

    return _success_response(
        {
            "ok": True,
            "status": "proposed",
            "proposed": result.proposed,
            "failed": [f.model_dump() for f in result.failed],
            "note": (
                f"{_PROPOSE_NOTE} Each draft is its own proposal — a human "
                "approves them one by one; there is no batch approval."
            ),
        }
    )


# ---------------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------------


def _build_tools() -> list[Any] | None:
    """Build the SDK tool objects, or ``None`` when the Claude Agent SDK isn't
    installed. Split out from ``build_growth_server`` so the gate test can walk
    every exposed tool's name and input schema without standing up a server."""
    try:
        from claude_agent_sdk import tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; %s MCP disabled", SERVER_NAME)
        return None

    @tool(
        "growth_list_prospects",
        (
            "List the workspace's outbound prospects as COMPACT rows (id, name, "
            "company, domain, tier, status, source) plus `total`, the count of "
            "every row matching the filters. Filter by `tier` (a/b/c/"
            "unqualified), `status`, `source`, and `q` (a search across name / "
            "company / domain / research brief). `sort` is newest (default) / "
            "oldest / company / tier. Pass the returned `next_cursor` back to "
            "read the next page. Use growth_get_prospect for the full record."
        ),
        {
            "type": "object",
            "properties": {
                "tier": {"type": "string", "enum": ["a", "b", "c", "unqualified"]},
                "status": {
                    "type": "string",
                    "enum": ["new", "qualified", "drafted", "in_sequence", "replied", "dead"],
                    "description": "Filter by prospect lifecycle status (a READ filter).",
                },
                "source": {"type": "string", "enum": ["clay", "directory", "manual"]},
                "q": {"type": "string", "description": "Search name/company/domain/brief."},
                "sort": {"type": "string", "enum": ["newest", "oldest", "company", "tier"]},
                "cursor": {"type": "string", "description": "The previous page's next_cursor."},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_LIST_LIMIT,
                    "description": f"Rows per page (default {_DEFAULT_LIST_LIMIT}).",
                },
            },
            "additionalProperties": False,
        },
    )
    async def growth_list_prospects(args):  # type: ignore[no-untyped-def]
        return await _list_prospects_handler(args)

    @tool(
        "growth_get_prospect",
        (
            "Read ONE prospect in full — research brief, emails, LinkedIn URL, "
            "WhatsApp number, opt-in and lifecycle status — together with every "
            "draft written for it (full copy). Call this before writing or "
            "revising copy so the message reflects what is already known and "
            "already said."
        ),
        {
            "type": "object",
            "properties": {"prospect_id": {"type": "string", "minLength": 1}},
            "required": ["prospect_id"],
            "additionalProperties": False,
        },
    )
    async def growth_get_prospect(args):  # type: ignore[no-untyped-def]
        return await _get_prospect_handler(args)

    @tool(
        "growth_list_drafts",
        (
            "List the workspace's drafts (newest first) with TRUNCATED body "
            "previews, optionally filtered by prospect, channel or status. Use "
            "it to collect draft ids — e.g. every draft still in `draft` — "
            "before proposing them. `status` is a read filter only; a draft "
            "moves status by being proposed and then approved by a human."
        ),
        {
            "type": "object",
            "properties": {
                "prospect_id": {"type": "string"},
                "channel": {"type": "string", "enum": ["email", "linkedin", "whatsapp"]},
                "status": {
                    "type": "string",
                    "enum": ["draft", "proposed", "approved", "sent", "replied", "rejected"],
                    "description": "Filter by draft status (a READ filter — it moves nothing).",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIST_LIMIT},
            },
            "additionalProperties": False,
        },
    )
    async def growth_list_drafts(args):  # type: ignore[no-untyped-def]
        return await _list_drafts_handler(args)

    @tool(
        "growth_upsert_prospect",
        (
            "Add a prospect you just researched, or enrich one that already "
            "exists. Keyed on the company `domain` — a domain already in the "
            "workspace is UPDATED, never duplicated. Fields you leave out keep "
            "their stored values, so you can add just an email or just a "
            "research brief. `emails` REPLACES the stored list when you pass "
            "it, so include the ones you want to keep. The prospect's lifecycle "
            "status is managed by the system and cannot be set here."
        ),
        {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Company website domain — the dedupe key.",
                },
                "name": {"type": "string", "description": "Contact name."},
                "company": {"type": "string"},
                "source": {
                    "type": "string",
                    "enum": ["clay", "directory", "manual"],
                    "description": "Where the prospect came from (default manual).",
                },
                "tier": {
                    "type": "string",
                    "enum": ["a", "b", "c", "unqualified"],
                    "description": "Qualification tier — a is the best fit.",
                },
                "research_brief": {
                    "type": "string",
                    "description": "What you found: the fit, the hook, the specific observation.",
                },
                "emails": {"type": "array", "items": {"type": "string"}},
                "linkedin_url": {"type": "string"},
                "whatsapp_number": {"type": "string"},
                "opted_in": {
                    "type": "boolean",
                    "description": "Whether the prospect opted in to WhatsApp contact.",
                },
            },
            "required": ["domain"],
            "additionalProperties": False,
        },
    )
    async def growth_upsert_prospect(args):  # type: ignore[no-untyped-def]
        return await _upsert_prospect_handler(args)

    @tool(
        "growth_create_draft",
        (
            "Write one channel's outreach copy for a prospect. The draft is "
            "saved as `draft` — nothing is queued and nothing sends. `subject` "
            "is email-only. `variant` is first_touch (default) or follow_up. "
            "When the copy is ready, call growth_propose_send to put it in "
            "front of a human."
        ),
        {
            "type": "object",
            "properties": {
                "prospect_id": {"type": "string", "minLength": 1},
                "channel": {"type": "string", "enum": ["email", "linkedin", "whatsapp"]},
                "body": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The message copy.",
                },
                "subject": {"type": "string", "description": "Email subject line (email only)."},
                "variant": {"type": "string", "enum": ["first_touch", "follow_up"]},
                "demo_url": {"type": "string", "description": "Optional demo link to include."},
            },
            "required": ["prospect_id", "channel", "body"],
            "additionalProperties": False,
        },
    )
    async def growth_create_draft(args):  # type: ignore[no-untyped-def]
        return await _create_draft_handler(args)

    @tool(
        "growth_update_draft",
        (
            "Revise a draft's copy — subject, body, demo_url, any subset. Only "
            "works while the draft is still `draft`: once it has been proposed, "
            "the stored copy is what a human is reviewing and what would be "
            "sent, so editing it is refused (draft.not_editable). To change "
            "proposed copy, have the human reject it and write a new draft."
        ),
        {
            "type": "object",
            "properties": {
                "draft_id": {"type": "string", "minLength": 1},
                "subject": {"type": "string"},
                "body": {"type": "string", "minLength": 1},
                "demo_url": {"type": "string"},
            },
            "required": ["draft_id"],
            "additionalProperties": False,
        },
    )
    async def growth_update_draft(args):  # type: ignore[no-untyped-def]
        return await _update_draft_handler(args)

    @tool(
        "growth_propose_send",
        (
            "PROPOSE sending a draft, for HUMAN APPROVAL. This does NOT send "
            "anything: it files the draft in the Instinct Tray and moves it to "
            "`proposed`. A human approves or rejects it there, and only an "
            "approved draft is picked up by the dispatch worker. Returns "
            "{status:'proposed', proposal_id}. NEVER tell the user a message "
            "was sent, delivered or is on its way — it is only PROPOSED."
        ),
        {
            "type": "object",
            "properties": {"draft_id": {"type": "string", "minLength": 1}},
            "required": ["draft_id"],
            "additionalProperties": False,
        },
    )
    async def growth_propose_send(args):  # type: ignore[no-untyped-def]
        return await _propose_send_handler(args)

    @tool(
        "growth_propose_send_batch",
        (
            "PROPOSE sending several drafts at once, for HUMAN APPROVAL — up to "
            "100 ids. Files ONE proposal per draft; a human still approves each "
            "in the Tray, and nothing is sent by this tool. A draft that cannot "
            "be proposed (already proposed, rejected, missing) comes back in "
            "`failed` and the rest still go. NEVER report these as sent."
        ),
        {
            "type": "object",
            "properties": {
                "draft_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 100,
                }
            },
            "required": ["draft_ids"],
            "additionalProperties": False,
        },
    )
    async def growth_propose_send_batch(args):  # type: ignore[no-untyped-def]
        return await _propose_send_batch_handler(args)

    @tool(
        "growth_linkedin_queue",
        (
            "Read the manual LinkedIn send queue: linkedin drafts in "
            "proposed/approved with the prospect context a human needs to send "
            "them by hand (name, company, profile URL, research brief, tier). "
            "LinkedIn sending is manual by design — there is no integration and "
            "no tool that sends."
        ),
        {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIST_LIMIT}},
            "additionalProperties": False,
        },
    )
    async def growth_linkedin_queue(args):  # type: ignore[no-untyped-def]
        return await _linkedin_queue_handler(args)

    return [
        growth_list_prospects,
        growth_get_prospect,
        growth_list_drafts,
        growth_upsert_prospect,
        growth_create_draft,
        growth_update_draft,
        growth_propose_send,
        growth_propose_send_batch,
        growth_linkedin_queue,
    ]


def build_growth_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for /growth, or ``None`` when the
    Claude Agent SDK isn't installed (chat must never break because of us)."""
    tools = _build_tools()
    if tools is None:
        return None

    from claude_agent_sdk import create_sdk_mcp_server

    server = create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=tools)
    return SERVER_NAME, server


__all__ = [
    "GROWTH_TOOL_IDS",
    "GROWTH_TOOL_NAMES",
    "MANAGE_ACTION",
    "READ_ACTION",
    "SERVER_NAME",
    "WRITE_ACTION",
    "build_growth_server",
]

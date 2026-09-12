# SenseResolver — binds a provider-agnostic Sense to a tenant's enabled connector.
# Created: 2026-06-08 — Sense tier chunk 2. A Sense (paw.email.v1) names a
# capability; this resolver picks the connector that fills it for a workspace,
# then executes via the EXISTING connectors_service path. READ-FIRST (v1): only
# ``trust_level == "auto"`` actions run; confirm/restricted (and unknown/missing
# trust) are BLOCKED and never sent to execute. Disambiguation: 0 candidates ->
# None (caller prompts-to-connect); 1 -> that connector; >1 -> the stored
# preference if it's among candidates, else the deterministic first with an
# ``ambiguous`` flag so the caller can ask the user to set a preference.
# Updated: 2026-06-08 (sense-tier efficiency fix) — extracted the disambiguation
#   (0/1/>1 + preference) into ``_disambiguate`` so ``resolve`` and the new
#   ``resolve_many`` share ONE implementation. ``resolve_many(sense_ids, ...)``
#   fetches the workspace's enabled connectors ONCE (one Beanie query for N
#   senses) and runs the pure intersection + shared disambiguation per id —
#   replacing the per-sense ``resolve`` loops in ``_check_template_needs`` and
#   the list_senses MCP handler. Behaviour is identical, just fewer queries.
# Updated: 2026-08-02 (Sense Phase 2, SP2-2) — the resolver now accepts an
#   AGENT tier above the stored preference rows. New ``AgentSenseContext``
#   (agent_id + carried mount list + per-sense provider prefs) threads as an
#   optional ``agent=`` keyword through ``resolve`` / ``resolve_many`` /
#   ``execute_sense`` / ``_disambiguate``. Disambiguation order is now: agent
#   pref (when it names a real candidate) -> the stored pocket-then-workspace
#   preference row -> deterministic first + ``ambiguous``. An agent pref naming
#   a NON-candidate (provider disabled, typo, another tenant's connector) is
#   skipped with one INFO log and falls through — never an error, so a stale
#   pref degrades to the old behaviour instead of breaking the sense. The
#   ``senses`` mount list is CARRIED but NOT consumed yet (SP2-3 gates on it).
#   ``agent=None`` is byte-for-byte the pre-SP2-2 path. The context is PURE
#   DATA on purpose: this module must never import the Beanie Agent document
#   (OSS-EE boundary) — the MCP layer loads the agent and hands the values in.
# Updated: 2026-08-02 (Sense Phase 2, SP2-3) — the MOUNT GATE. The carried
#   ``senses`` tuple is now CONSUMED: an agent whose mount list is non-empty
#   reaches EXACTLY those senses and nothing else (the ``tool_mode="exclusive"``
#   rule applied to capabilities). An EMPTY mount list still inherits the whole
#   workspace surface, so every pre-SP2-3 caller is unchanged.
#   The gate is a pure predicate, ``is_sense_carried(sense_id, agent)``, applied
#   BEFORE any candidate work — no registry build, no Beanie read, no preference
#   lookup for a sense the agent doesn't carry. ``resolve_many`` skips
#   non-carried ids for free (the batch's single enabled-connector read still
#   happens once for whatever remains).
#   RETURN SHAPE — the deliberate choice: ``resolve`` / ``resolve_many`` keep
#   returning ``None`` for a non-carried sense, exactly as they do for "no
#   provider". Their contract is ``ResolvedSense | None`` and every existing
#   caller branches on ``is None``; a truthy-but-different sentinel would make
#   an un-updated caller read ``.connector_name`` off a refusal, and a raise
#   would turn a policy decision into an exception path through code that
#   currently cannot fail. Callers that must tell the two APART ask the
#   predicate — it is pure, free, and needs no I/O. ``execute_sense``, which
#   already owns a structured envelope, does exactly that and returns the stable
#   ``error="sense.not_carried"`` code (namespaced like its siblings
#   ``sense.no_provider`` / ``sense.action_needs_approval``).

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from pocketpaw.senses import validate_sense_id
from pocketpaw_ee.cloud.connectors import service as connectors_service
from pocketpaw_ee.cloud.connectors.dto import ExecuteActionRequest
from pocketpaw_ee.cloud.senses import preference
from pocketpaw_ee.cloud.senses.filler import ConnectorSenseFiller

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentSenseContext:
    """The sense-relevant slice of the Agent driving the current run.

    Pure data — the caller (the MCP layer) loads the Agent and hands the values
    across, so the resolver never imports a Beanie document.

    ``prefs`` maps sense_id -> connector_name and outranks the stored
    pocket/workspace preference rows; a pref naming a connector that isn't a
    candidate is skipped, not an error.

    ``senses`` is the agent's MOUNT LIST. Empty (the default) inherits every
    sense the workspace can fill; NON-EMPTY is EXCLUSIVE — the agent reaches
    those senses and only those, and everything else refuses with
    ``sense.not_carried`` before any candidate lookup. Prefs never widen this:
    a pref for a sense outside the mount list is dead config.
    """

    agent_id: str
    senses: tuple[str, ...] = ()
    prefs: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedSense:
    """The connector chosen to fill a sense for a workspace.

    ``ambiguous`` is True when more than one enabled connector could fill the
    sense and no stored preference disambiguated it — the caller should surface
    a "set a preference" prompt. ``candidates`` is the full sorted candidate
    set so the caller can render the choices.
    """

    sense_id: str
    connector_name: str
    ambiguous: bool = False
    candidates: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SenseExecutionResult:
    """Envelope for ``execute_sense``.

    Structured (never an HTTP 500): ``ok`` is False with a stable ``error``
    code for the three refusal paths — the agent doesn't carry the sense
    (SP2-3), no provider fills it for the workspace, and the read-first block
    on a non-auto action. On success ``data`` carries the underlying
    ``ExecuteActionResponse`` and ``connector_name`` records which provider ran.
    Only the read-first refusal names a ``connector_name``; the other two are
    refused before one is chosen.
    """

    ok: bool
    sense_id: str
    connector_name: str | None = None
    action: str | None = None
    error: str | None = None  # stable code — one of the SENSE_* constants below
    message: str | None = None
    data: object = None


# Stable, caller-facing refusal codes for ``SenseExecutionResult.error``.
SENSE_NOT_CARRIED = "sense.not_carried"
SENSE_NO_PROVIDER = "sense.no_provider"
SENSE_ACTION_NEEDS_APPROVAL = "sense.action_needs_approval"


def is_sense_carried(sense_id: str, agent: AgentSenseContext | None) -> bool:
    """Does this agent carry ``sense_id``? — the whole mount rule, in one place.

    True when there is no agent context (legacy / OSS callers), when the agent's
    mount list is EMPTY (inherit the workspace's full surface), or when the sense
    is named in it. False only for the exclusive case: a non-empty mount list
    that doesn't include this sense.

    Pure and I/O-free on purpose. It is the gate ``resolve`` / ``resolve_many``
    apply BEFORE touching the registry, and the predicate callers use to tell a
    ``None`` that means "not carried" from one that means "no provider" — the
    two cases ``resolve``'s ``ResolvedSense | None`` contract cannot distinguish
    on its own.
    """
    if agent is None or not agent.senses:
        return True
    return sense_id in agent.senses


def _not_carried_message(sense_id: str, agent: AgentSenseContext) -> str:
    """The refusal text for a non-carried sense — names what the agent DOES
    carry so the model stops retrying the one it can't reach."""
    carried = ", ".join(agent.senses)
    return (
        f"sense {sense_id!r} is not mounted on this agent ({SENSE_NOT_CARRIED}) — "
        f"it carries: {carried}. Use one of those, or ask the workspace owner to "
        "mount this capability on the agent."
    )


async def _disambiguate(
    sense_id: str,
    candidates: list[str],
    workspace_id: str,
    *,
    pocket_id: str | None = None,
    agent: AgentSenseContext | None = None,
) -> ResolvedSense | None:
    """Pick the connector from a sorted candidate set — the ONE rule both
    ``resolve`` and ``resolve_many`` use.

    0 candidates -> ``None`` (caller prompts-to-connect). Otherwise, in order:
    the running AGENT's own pref for this sense (SP2-2) when it names one of the
    candidates; then a single candidate short-circuits; then the stored
    pocket/workspace preference; else the deterministic sorted-first with
    ``ambiguous=True``.

    An agent pref that is NOT among the candidates is skipped with one INFO log
    and falls through to the stored preference — a disabled or misnamed provider
    degrades to the pre-agent behaviour instead of failing the sense. The stored
    preference lookup still only runs on the rare >1 branch.
    """
    if not candidates:
        return None

    # Agent tier — the running agent's own choice outranks the stored rows.
    agent_pref = agent.prefs.get(sense_id) if agent is not None else None
    if agent_pref is not None:
        if agent_pref in candidates:
            return ResolvedSense(
                sense_id=sense_id,
                connector_name=agent_pref,
                ambiguous=False,
                candidates=candidates,
            )
        logger.info(
            "agent sense pref skipped: agent=%s sense=%s pref=%s not in candidates=%s",
            agent.agent_id,
            sense_id,
            agent_pref,
            candidates,
        )

    if len(candidates) == 1:
        return ResolvedSense(
            sense_id=sense_id,
            connector_name=candidates[0],
            ambiguous=False,
            candidates=candidates,
        )

    # More than one candidate — let a stored preference decide.
    preferred = await preference.get_preference(workspace_id, sense_id, pocket_id=pocket_id)
    if preferred is not None and preferred in candidates:
        return ResolvedSense(
            sense_id=sense_id,
            connector_name=preferred,
            ambiguous=False,
            candidates=candidates,
        )

    # No usable preference — pick deterministically and flag for the caller.
    return ResolvedSense(
        sense_id=sense_id,
        connector_name=candidates[0],
        ambiguous=True,
        candidates=candidates,
    )


async def resolve(
    sense_id: str,
    workspace_id: str,
    *,
    pocket_id: str | None = None,
    agent: AgentSenseContext | None = None,
) -> ResolvedSense | None:
    """Bind ``sense_id`` to the connector that fills it for this workspace.

    Returns ``None`` when no enabled connector can fill the sense (the caller
    decides what to do — typically prompt-to-connect) AND when the agent doesn't
    carry the sense (SP2-3); ``is_sense_carried`` tells the two apart without a
    query. Raises ``SenseValidationError`` for an unknown ``paw.*`` id. Pass
    ``agent`` to apply the mount gate and let the running agent's own provider
    pref outrank the stored preference rows; ``agent=None`` is the unchanged
    pre-SP2-2 behaviour.
    """
    validate_sense_id(sense_id)

    # Mount gate (SP2-3) — refuse BEFORE any candidate work. An id the agent
    # doesn't carry costs no registry build and no Beanie read.
    if not is_sense_carried(sense_id, agent):
        logger.info(
            "sense not carried: agent=%s sense=%s mounted=%s",
            agent.agent_id,  # type: ignore[union-attr] — non-None whenever the gate trips
            sense_id,
            agent.senses,  # type: ignore[union-attr]
        )
        return None

    registry = connectors_service._get_registry()  # noqa: SLF001 — reuse the EE singleton
    filler = ConnectorSenseFiller(registry)
    candidates = await filler.candidates(sense_id, workspace_id, pocket_id=pocket_id)
    return await _disambiguate(sense_id, candidates, workspace_id, pocket_id=pocket_id, agent=agent)


async def resolve_many(
    sense_ids: list[str],
    workspace_id: str,
    *,
    pocket_id: str | None = None,
    agent: AgentSenseContext | None = None,
) -> dict[str, ResolvedSense | None]:
    """Resolve many senses with ONE enabled-connector read.

    Fetches the workspace's enabled connector names a single time, then runs
    the PURE intersection + the SAME ``_disambiguate`` rule ``resolve`` uses for
    each id. Equivalent to calling ``resolve`` per id, but it collapses N
    identical enabled-connector queries into one. Returns a dict mapping every
    input id to its ``ResolvedSense`` (or ``None`` when no provider fills it).

    Raises ``SenseValidationError`` if any id is not a known ``paw.*`` sense
    (validated up front, same as ``resolve``). Duplicate ids collapse to one
    key. The >1-candidate preference lookup still runs per ambiguous id — that
    branch is rare, so it stays as-is.

    Mount gate (SP2-3): a sense the ``agent`` doesn't carry maps to ``None``
    without ANY candidate work — the intersection is skipped, not just the
    disambiguation. Every input id still appears in the result, so the caller's
    key set is unchanged. ``is_sense_carried`` distinguishes those ``None``s
    from the no-provider ones.
    """
    for sense_id in sense_ids:
        validate_sense_id(sense_id)

    # Nothing in the batch is carried — skip the batch read too, not just the
    # per-sense intersection.
    if not any(is_sense_carried(sense_id, agent) for sense_id in sense_ids):
        return dict.fromkeys(sense_ids)

    registry = connectors_service._get_registry()  # noqa: SLF001 — reuse the EE singleton
    filler = ConnectorSenseFiller(registry)
    # The ONE Beanie read shared across every sense in the batch.
    enabled_names = await filler.enabled_connector_names(workspace_id, pocket_id=pocket_id)

    out: dict[str, ResolvedSense | None] = {}
    for sense_id in sense_ids:
        if not is_sense_carried(sense_id, agent):
            out[sense_id] = None
            continue
        candidates = filler.candidates_from(sense_id, enabled_names)
        out[sense_id] = await _disambiguate(
            sense_id, candidates, workspace_id, pocket_id=pocket_id, agent=agent
        )
    return out


def _trust_for_action(registry, connector_name: str, action: str) -> str | None:
    """Read ``trust_level`` for an action from the connector's ConnectorDef.

    Returns the trust string (e.g. "auto" | "confirm" | "restricted") or
    ``None`` when the connector, the action, or the trust field is missing.
    ``None`` is treated by the gate as "not auto" -> blocked.
    """
    defn = registry.get_definition(connector_name)
    if defn is None:
        return None
    for a in getattr(defn, "actions", None) or []:
        if isinstance(a, dict) and a.get("name") == action:
            return a.get("trust_level")
    return None


async def execute_sense(
    sense_id: str,
    action: str,
    params: dict,
    workspace_id: str,
    *,
    pocket_id: str | None = None,
    user_id: str | None = None,
    agent: AgentSenseContext | None = None,
) -> SenseExecutionResult:
    """Resolve a sense, enforce the read-first gate, then delegate to execute.

    0. MOUNT GATE (SP2-3) — if the agent carries a non-empty mount list and this
       sense isn't in it, refuse with ``sense.not_carried`` before resolving.
       This runs FIRST: an un-carried sense must refuse identically whether or
       not the workspace happens to have a provider for it, so the refusal never
       leaks which connectors the tenant enabled.
    1. ``resolve`` — if no provider, return a structured ``sense.no_provider``
       result (never raise a 500).
    2. READ-FIRST GATE — the action's ``trust_level`` on the resolved
       connector must be exactly ``"auto"``. Anything else (confirm /
       restricted / unknown / missing) is refused with
       ``sense.action_needs_approval`` and ``connectors_service.execute`` is
       NEVER called.
    3. Delegate to the existing execute path with the resolved connector.

    ``agent`` (SP2-2) rides into step 1 so the agent's own provider pref picks
    the connector this execution runs against. The read-first gate is unchanged
    and applies to whichever connector wins.
    """
    validate_sense_id(sense_id)
    if not is_sense_carried(sense_id, agent):
        return SenseExecutionResult(
            ok=False,
            sense_id=sense_id,
            action=action,
            error=SENSE_NOT_CARRIED,
            message=_not_carried_message(sense_id, agent),  # type: ignore[arg-type]
        )

    resolved = await resolve(sense_id, workspace_id, pocket_id=pocket_id, agent=agent)
    if resolved is None:
        return SenseExecutionResult(
            ok=False,
            sense_id=sense_id,
            action=action,
            error=SENSE_NO_PROVIDER,
            message=(
                f"no enabled connector can fill {sense_id!r} for this workspace — "
                "connect a provider for this sense and retry."
            ),
        )

    connector_name = resolved.connector_name
    registry = connectors_service._get_registry()  # noqa: SLF001 — reuse the EE singleton
    trust = _trust_for_action(registry, connector_name, action)
    if trust != "auto":
        return SenseExecutionResult(
            ok=False,
            sense_id=sense_id,
            connector_name=connector_name,
            action=action,
            error=SENSE_ACTION_NEEDS_APPROVAL,
            message=(
                f"action {action!r} on {connector_name!r} needs approval "
                f"(trust_level={trust!r}) — not executed in v1 (read-first)."
            ),
        )

    response = await connectors_service.execute(
        workspace_id,
        connector_name,
        ExecuteActionRequest(action=action, params=params, pocket_id=pocket_id),
        user_id=user_id,
    )
    return SenseExecutionResult(
        ok=True,
        sense_id=sense_id,
        connector_name=connector_name,
        action=action,
        data=response,
    )


__all__ = [
    "SENSE_ACTION_NEEDS_APPROVAL",
    "SENSE_NOT_CARRIED",
    "SENSE_NO_PROVIDER",
    "AgentSenseContext",
    "ResolvedSense",
    "SenseExecutionResult",
    "execute_sense",
    "is_sense_carried",
    "resolve",
    "resolve_many",
]
